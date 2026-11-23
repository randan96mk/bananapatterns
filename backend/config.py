"""Central configuration.

Secrets (Kite credentials) come from .env and are never persisted.
Tunable sections are dataclasses saved to the `app_settings` table as JSON
and editable at runtime through /api/config/{section}. Phase 2 adds a `vcp`
section here for the screener dials using the same mechanism.
"""

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(override=False)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
CONFIG_DIR = BASE_DIR / "config"

try:
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # tzdata missing (rare on macOS/Linux)
    IST = timezone(timedelta(hours=5, minutes=30), "IST")


def db_path() -> Path:
    """SQLite file location; BP_DB_PATH overrides it (used by tests)."""
    p = os.getenv("BP_DB_PATH")
    return Path(p) if p else DATA_DIR / "bananapatterns.db"


@dataclass
class KiteConfig:
    api_key: str = field(default_factory=lambda: os.getenv("KITE_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("KITE_API_SECRET", ""))
    access_token: str = field(default_factory=lambda: os.getenv("KITE_ACCESS_TOKEN", ""))
    redirect_url: str = field(
        default_factory=lambda: os.getenv("KITE_REDIRECT_URL", "http://localhost:8000/api/kite/callback")
    )

    @property
    def is_configured(self) -> bool:
        return len(self.api_key) >= 6 and self.api_key != "your_api_key" and bool(self.api_secret) \
            and self.api_secret != "your_api_secret"


@dataclass
class IngestConfig:
    """Data ingestion dials."""
    history_years: int = 8                 # backfill depth (screener needs ~2y, backtester wants more)
    hist_rate_per_sec: float = 3.0         # Kite historical API limit is 3 req/s
    max_retries: int = 5                   # retries for network / 429 / 5xx
    backoff_base_sec: float = 1.0          # exponential backoff: base * 2^attempt (+ jitter)
    chunk_days: int = 2000                 # Kite max range per call for "day" candles
    overlap_bars: int = 5                  # incremental refetch overlap to detect re-adjustment
    adjust_tolerance: float = 0.005        # >0.5% close mismatch on overlap => full refetch (split/bonus)
    min_bars_warn: int = 260               # fewer bars => flagged (new listing, IPO-base screen later)
    eod_ready_time: str = "15:45"          # IST; before this, today's daily bar is partial and is skipped
    universe_urls: list = field(default_factory=lambda: [
        "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
        "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    ])
    # Constituent series to ingest: EQ, plus BE (trade-for-trade equities, listed on Kite as SYMBOL-BE).
    # REITs (RR) are index members but not equities, so they are excluded; they stay in the snapshot.
    include_series: list = field(default_factory=lambda: ["EQ", "BE"])
    # Index tradingsymbols on Kite (segment INDICES) used for RS and market regime
    index_symbols: list = field(default_factory=lambda: ["NIFTY 500", "NIFTY 50"])


@dataclass
class ServerConfig:
    host: str = field(default_factory=lambda: os.getenv("BP_HOST", "127.0.0.1"))   # Docker: 0.0.0.0
    backend_port: int = 8000
    frontend_port: int = 3000


@dataclass
class BaseConfig:
    """Base (consolidation box) detection dials. bananapatterns.com publishes no cut-offs; these are ours."""
    pivot_basis: str = "close"             # pivot = highest "close" in the base (matches the site) or "high"
    min_base_bars: int = 5                 # 1 week; the site draws 1-2 week boxes (VCP screen will ask for more)
    max_base_bars: int = 325               # 65 weeks; longer ranges belong to the multi-year screen (later)
    max_base_depth: float = 0.30           # (pivot - base low) / pivot; 0.30 + min_prior_advance 0.10 reproduce
                                           # the site's VCP Forming bases (6/6 exact, 23 Oct 2026)
    max_pivot_drift: float = 0.03          # pivot_basis="high" only: wicks may lift the pivot this far above the
                                           # base's first high; beyond it the stock is grinding up (a trend)
    prior_advance_bars: int = 63           # look back this far for the advance into the base
    min_prior_advance: float = 0.10        # base high >= 10% above the lowest low of that lookback
    reset_ma: int = 200                    # a close below this SMA restarts the base count at B1 (0 = off)
    reset_on_undercut: bool = True         # a base low under the previous base low restarts the count
    vol_baseline: str = "sma50"            # breakout volume vs "sma50" (brief) or "median20" (site's card)
    bo_vol_mult: float = 1.5               # volume_ok when breakout volume >= mult x baseline
    weak_close_cir: float = 0.5            # close-in-range below this = weak close
    squat_bars: int = 5                    # a close back under the pivot within N bars = squat
    dryup_lookback: int = 50               # bars before the base used for the volume dry-up ratio


@dataclass
class MarketConfig:
    """Market health (computed with each scan from the Nifty 500 and its stocks)."""
    breadth_up: float = 50.0               # % of stocks above their 50-day line for an uptrend
    breadth_correction: float = 35.0       # under this (with the index under its 50-day) = correction
    dist_window: int = 25                  # sessions counted for distribution days
    dist_drop_pct: float = 0.2             # index down at least this % ...
    dist_days_max: int = 4                 # ... on higher volume: more than this = no uptrend
    dist_days_correction: int = 7          # this many or more = correction
    breakout_window: int = 20              # breakouts in the last N sessions ...
    breakout_rate_min: float = 0.5         # ... at least this share still above the pivot for an uptrend
    # 📈 the market read on Tips (backend/market): the chart facts by code always show; the Kronos candle model runs in its
    # own process on this server (127.0.0.1:port) and its numbers stay in shadow until the scorecard earns them
    kronos: bool = True                    # the candle model worker runs and reads NIFTY / BANKNIFTY / SENSEX + open Kite hints
    kronos_show: str = "shadow"            # shadow (stored and scored, never shown beyond the hit-rate chip) | cards (shown once the gate passes)
    interval_s: int = 60                   # seconds between index reads 9:15 AM–3:30 PM (30 at least)
    horizon_min: int = 30                  # the horizon the panel and the cards headline (15 and 30 are both computed)
    samples: int = 10                      # sampled paths per read
    port: int = 8765                       # the worker's port on 127.0.0.1


@dataclass
class ScreenConfig:
    """Stage-board dials (Forming / Fresh breakouts / Climbing / Played out)."""
    min_forming_weeks: int = 3             # Forming: active base at least this long
    max_dist_to_pivot: float = 0.05        # Forming: close within 5% under the pivot
    fresh_sessions: int = 5                # Fresh: broke out within the last N sessions (incl. today)
    require_volume: bool = True            # Fresh/Climbing need a volume-confirmed breakout (bases.bo_vol_mult)
    played_out_ma: int = 50                # run ends on a close below this SMA (0 = off)
    played_out_drop: float = 0.08          # run ends on a close this far below the pivot
    played_out_window: int = 20            # Played out: run ended within the last N sessions
    trend_ma: int = 200                    # all cards: close above this SMA (0 = off)
    min_turnover_cr: float = 5.0           # all cards: median daily turnover (Rs crore) at least this
    turnover_window: int = 50              # sessions for the median turnover


@dataclass
class TrendConfig:
    """RS rating and Minervini Trend Template dials."""
    rs_weights: list = field(default_factory=lambda: [0.4, 0.2, 0.2, 0.2])   # weights for 3/6/9/12-month returns
    rs_periods: list = field(default_factory=lambda: [63, 126, 189, 252])     # sessions per period
    rs_min_bars: int = 63                  # fewer bars => no RS (weights renormalised over available periods)
    ma_fast: int = 50
    ma_mid: int = 150
    ma_slow: int = 200
    slow_rising_bars: int = 21             # 200-day SMA higher than this many sessions ago (21 ~ 1 month)
    min_above_low52: float = 0.30          # close >= 30% above the 52-week low
    max_below_high52: float = 0.25         # close within 25% of the 52-week high
    min_rs: int = 70                       # RS rating at least this


@dataclass
class FlagConfig:
    """Bull flag / high-tight flag: a sharp pole, then a short, quiet pullback. Bars are trading sessions."""
    min_pole_gain: float = 0.20            # pole: low -> top at least +20% ...
    pole_max_bars: int = 30                # ... within this many sessions
    pole_min_bars: int = 3
    pole_min_volume: float = 1.2           # pole volume / 50-day average before it
    flag_min_bars: int = 5
    flag_max_bars: int = 25
    max_flag_depth: float = 0.25           # flag low at most this far under the pole top
    max_retrace: float = 0.38              # ... and at most this share of the pole given back
    tight_retrace: float = 0.25            # "tight" tag (and full tightness score) at or under this
    max_flag_volume: float = 0.8           # flag volume / pole volume (dry-up)
    max_flag_rise: float = 0.03            # the flag's fitted line may rise at most this much: a flag drifts
    htf_min_gain: float = 0.90             # high-tight flag: pole +90% ...
    htf_max_bars: int = 40                 # ... within 40 sessions, flag no deeper than max of 25%
    max_dist_to_pivot: float = 0.06        # Forming: close within this under the flag high
    stop_buffer: float = 0.01              # stop = flag low - 1%, never more than max_stop under the pivot
    max_stop: float = 0.10
    min_rs: int = 80
    min_trend_checks: int = 5
    history_bars: int = 300                # how far back breakouts are looked for


@dataclass
class VcpConfig:
    """VCP screen dials (classic Minervini VCP; metric definitions in backend/engine/vcp.py).

    The site's own VCP list is looser (e.g. its latest pullback can be the deepest); relax
    min_contractions / require_shrinking / max_final_contraction to approximate it.
    """
    max_depth: float = 0.30                # base depth (pivot -> base low)
    min_weeks: int = 3                     # base at least this long
    min_contractions: int = 2              # pullbacks inside the base (brief: 2-4, up to 6)
    max_contractions: int = 6
    require_shrinking: bool = True         # last contraction <= shrink x the largest earlier one
    shrink: float = 0.8
    max_final_contraction: float = 0.12    # last pullback at most 12% deep
    max_tightening: float = 1.0            # ATR(recent) / base mean true range
    max_dryup: float = 0.85                # recent volume / volume before the base
    min_rs: int = 70
    min_trend_checks: int = 5              # of the 8 Trend Template checks
    max_dist_to_pivot: float = 0.25        # Forming card (site's VCP names sit 9-21% under their pivots)
    atr_recent: int = 10
    dryup_recent: int = 10
    dryup_lookback: int = 50
    ud_window: int = 50
    zz_min_pct: float = 0.05               # ZigZag reversal threshold floor (median 3 contractions on Nifty 500)
    zz_atr_mult: float = 2.0               # ... or this many ATR%, whichever is larger
    stop_buffer: float = 0.01              # stop just under the last contraction low
    max_stop: float = 0.08                 # never more than 8% under the pivot
    score_weights: list = field(default_factory=lambda: [30.0, 20.0, 15.0, 15.0, 10.0, 10.0])
    # weights: RS, tightening, dry-up, proximity to pivot, proximity to 52w high, industry RS (untuned until backtest)


@dataclass
class AlertsConfig:
    """Telegram alerts and the automatic jobs. The bot token lives in .env (TELEGRAM_BOT_TOKEN)."""
    enabled: bool = True
    chat_id: str = ""                      # found with "Detect chat ID" (or set TELEGRAM_CHAT_ID in .env)
    screens: list = field(default_factory=lambda: ["vcp", "flag"])
    breakout: bool = True                  # fresh breakout (volume-confirmed)
    low_volume_breakout: bool = False
    below_pivot: bool = True
    played_out: bool = True
    forming: bool = True                   # newly forming
    left: bool = False                     # left a card (noisy)
    near_pivot: bool = True
    near_pivot_pct: float = 0.03           # forming and within 3% under the pivot (once per base)
    digest: bool = True
    chart_images: bool = True              # annotated decision chart with the alert types in chart_kinds
    chart_kinds: list = field(default_factory=lambda: ["breakout", "breakout_low_volume", "near_pivot", "forming",
                                                       "below_pivot", "played_out", "climbing", "intraday"])
    buy_zone_pct: float = 0.05             # buy zone = pivot .. +5% (beyond it: extended, don't chase)
    capital: float = 0.0                   # position size on the chart: 0 = off
    risk_pct: float = 0.01                 # ... risking this share of capital per trade (0.01 = 1%)
    system: bool = True                    # login reminder, job failures, index changes
    intraday: bool = True                  # heads-up when price crosses a forming VCP's pivot
    intraday_interval_min: int = 5
    intraday_start: str = "09:20"
    intraday_end: str = "15:25"
    intraday_volume_mult: float = 1.5      # projected day volume needed for "strong" / "holding" (x 50-day avg)
    failed_buffer_pct: float = 0.01        # after crossing, falling this far under the pivot = failed breakout
    close_check: bool = True               # act-before-close check
    close_check_time: str = "15:15"
    close_min_range: float = 0.5           # (price - day low) / day range needed at the close check
    job_enabled: bool = True               # automatic evening sync + scan + alerts
    job_time: str = "18:30"                # IST, trading days
    reminder_time: str = "15:30"           # IST: remind if today's Kite login is missing
    universe_refresh_weekday: int = 6      # 0=Mon ... 6=Sun; weekly universe + holiday refresh
    # Kite login links (Telegram button). Tokens die at 06:00 IST, so a login only lasts the day if made after it.
    login_links: bool = True
    login_times: list = field(default_factory=lambda: ["06:01", "08:45"])   # IST, each >= 06:00; sent if not logged in
    login_heads_up: bool = False           # "session ends at 06:00" note while a token is still active
    login_heads_up_time: str = "05:45"
    login_every_day: bool = False          # also on weekends / holidays
    login_check_min: int = 30              # ask Kite every N min whether the session is still accepted
    # A/B priority and the market header
    market_line: bool = True               # market status line on every alert
    tiering: bool = True                   # A setups alert at once; B setups per b_alerts
    a_min_rs: int = 85
    a_min_trend: int = 7                   # Trend Template checks passed (of 8)
    a_top_industry_frac: float = 0.33      # industry rank within the top third
    b_alerts: str = "digest"               # digest (listed in the digest only) / push (send, tagged B) / off
    alert_style: str = "summary"           # summary: the evening's alerts in one 7 PM report (charts on tap) /
                                           # every: each alert its own message, as before
    correction_mode: str = "caution"       # caution (A alerts still sent, with a warning) / mute_buys
    market_sizing: bool = True             # position size x1 uptrend, x0.5 under pressure, x0.25 correction
    # Daily plan: tonight's top setups, the morning plan, the pre-open gap check
    plan_top_n: int = 5
    top_setups: bool = True
    top_setups_time: str = "19:00"         # after the evening scan (waits for it, until 23:00)
    morning_plan: bool = True
    morning_plan_time: str = "08:50"
    gap_check: bool = True
    gap_check_time: str = "09:10"          # after the 09:00-09:08 pre-open session sets the open
    coiled_volume: float = 0.5             # coiled: last day's volume <= this x 50-day average ...
    coiled_within_pct: float = 3.0         # ... within this % under the pivot, on an NR7 or inside day
    alert_buttons: bool = True             # Watch / Mute / Chart / I bought under stock alerts
    mute_days: int = 30
    profit_zone_pct: float = 20.0          # gain from your average: consider selling part
    extended_pct: float = 25.0             # close this far above the 50-day line: extended
    failed_days: int = 3                   # back under the pivot within N sessions of buying: failed breakout
    # Live guard (market hours, read-only): positions checked on every intraday tick
    live_guard: bool = True
    live_drop_pct: float = 4.0             # heavy selling: down this % or more today ...
    live_drop_volume: float = 2.0          # ... on this x normal volume for the time of day (still above the stop)
    hard_stop: bool = False                # under-stop alert says "exit now" (else: unconfirmed until the close)
    # Entry checklist on intraday heads-ups and the 15:15 check, and the Today page
    entry_checklist: bool = True           # zone / VWAP / volume pace / vs Nifty 500 / opening range -> verdict
    opening_range_min: int = 30            # "over the first N minutes' high"
    today_near_pct: float = 3.0            # Today page: Forming names within this % under their pivot
    # Results calendar (NSE board meetings, fetched in the evening job): gap risk around results
    results_calendar: bool = True          # fetch the calendar and show "Results ..." on cards, alerts and plan
    results_warn_sessions: int = 5         # stock alerts / plan / cards warn when results are this close
    results_guard_sessions: int = 1        # guard: warn on a held stock this many sessions before its results
    # Exchange filings (NSE corporate announcements): news lines on alerts, 📰 on cards, filing alerts
    filings: bool = True
    filings_interval_min: int = 15         # fetched every N minutes, 07:00-23:00
    filings_weekend_from: str = "18:00"    # weekends: off on Saturday, on Sunday only from this time (nothing to act on before)
    filings_alerts: bool = True            # push important filings on stocks you watch, hold or have in the plan
    news_days: int = 3                     # "recent" for alert news lines and card chips (calendar days)
    # Red flags and fund buying (filings + bulk / block deals + SAST holder disclosures)
    red_flags: bool = True                 # 🚩 promoter selling, legal action, auditor exit, downgrade: tier B
    red_flag_days: int = 30
    stake_change_min_pct: float = 1.0      # SAST: a promoter sale / fund stake change (% of the company) that counts
    deals: bool = True                     # 🏦 funds buying / selling in bulk / block deals or raising stakes
    deal_days: int = 10
    deal_min_cr: float = 5.0               # net bulk / block value (₹ crore) a fund must trade to count
    # 🌙 one close-of-day message across the books (Real, Paper, Options, the AI)
    day_summary: bool = True               # the day summary on trading days (replaces the options day line)
    day_summary_time: str = "15:40"        # after the options book's 3:35 PM summary row


@dataclass
class BreakoutConfig:
    """Trend Breakout E+ (swing, long only, delivery) on the Nifty 500 from daily candles: the trend template and a
    breakout over the 20-day high on 1.5x volume, a fixed stop, the late trail, out on a close under the 50-day
    average; new buys under the half market filter. Ported from randan96mk/Backtest swing.py (the "breakout"
    strategy). Backtest and evening scan only: never orders. (Replaced the ORB + VWAP in Oct 2026: its saved settings
    are dropped. Variant A, the 200-day filter on the top 200, was removed in Nov 2026: a saved config's `top_e`,
    `late_at_e` and `late_pct_e` are read into `top`, `late_at` and `late_pct` once.)"""
    test_years: float = 6.0                # sessions tested (after warm-up), ending at the latest NIFTY 50 candle
    warmup_years: float = 1.2              # history loaded before them (the first warmup_sessions are warm-up)
    warmup_sessions: int = 260
    capital: float = 300_000.0
    max_pos: int = 10                      # equal weight: equity / max_pos
    top: int = 300                         # universe: the most traded N (20-day average turnover)
    min_price: float = 100.0
    vol_mult: float = 1.5                  # volume vs the 50-day average
    stop_pct: float = 8.0                  # stop = max(close - atr_mult x ATR(14), close x (1 - stop_pct %))
    atr_mult: float = 2.0
    hi_window: int = 20                    # breakout over the previous N-day high (10, 20 or 55)
    exit_sma: int = 50                     # out at the next open after a close under this average (20 or 50)
    regime: bool = True                    # market filter on: new buys only while NIFTY 50 allows them (see regime_mode)
    slippage_pct: float = 0.10             # each fill
    stress_slippage_pct: float = 0.25
    sell_pct: float = 8.0                  # evening scan: a holding this far under its average price is a sell
    late_at: float = 100.0                 # E+ late trailing stop: active once the best close is late_at % over the
    late_pct: float = 25.0                 # entry; stop = best close x (1 - late_pct %), only ever raised (0 = off)
    regime_mode: str = "half"              # market filter: half (E+: 10 slots over the 200-day SMA, 5 over the 50-day,
                                           # none under it); sma200 (all or nothing) stays available to the engine only


@dataclass
class PaperConfig:
    """Paper trading of the Trend Breakout rules (simulated, never orders). The capital is used when paper trading
    starts (or restarts after a reset); sizes, quantities and cash follow from it and from the account's value."""
    capital: float = 300_000.0             # per account
    variants: list = field(default_factory=lambda: ["E"])        # E+ is the only variant (A removed Nov 2026);
                                                                 # unknown entries in a saved config are ignored


@dataclass
class RealConfig:
    """💰 Real trading: the Trend Breakout orders sent to Tradetron by webhook (BUY / EXIT), tracked like paper
    trading. Prices come from Kite (read-only); no order ever goes to Kite."""
    enabled: bool = True                   # send orders (the kill switch on the tab overrides it)
    variant: str = "E"                     # strategy for NEW buys: E (E+, the only variant since Nov 2026)
    capital: float = 300_000.0             # set on the tab; a later change counts as money added / withdrawn
    preview_time: str = "09:00"            # a Telegram line listing the day's orders before they go ("" = off)
    send_time: str = "09:16"               # the day's orders (EXITs, then BUYs) go out from here...
    send_until: str = "09:30"              # ...until here (missed: not sent that day, with an alert)
    stop_check_min: int = 5                # stops watched with live prices every N minutes in market hours
    max_buys_per_day: int = 5
    send_gap_s: float = 10.0               # seconds between two webhooks (1–30): one engine cycle apart, so none is overwritten
    send_stop: bool = False                # also send each BUY's stop to Tradetron (sig_stop) so Tradetron can exit on its
                                           # own; turn on once the Tradetron strategy reads sig_stop
    nologin_alarm_min: int = 30            # positions open, no Kite login in market hours: a loud alarm every N minutes
    max_order_pct: float = 15.0            # a BUY above this % of the account value is cut to fit (15% of Rs 3L = 45,000)
    results_guard_sessions: int = 3        # a planned BUY whose results date is within N sessions is pre-skipped for the
                                           # morning (switch it back on from the Real page or Telegram); 0 = off
    tt_sid: int = 0                        # the webhook side's deployment id for the read-only fill check (0 = not set)
    confirm_time: str = "10:00"            # the fill check on trading days (24-hour HH:MM, IST; "" = off); again in the evening
    confirm_alert: bool = True             # Telegram: the fill check's confirmations and the evening "Real vs webhook side"
    exit_needs_confirm: bool = False       # the EXIT gate: on = an unconfirmed buy never gets an EXIT (alert instead); off = exits go out as always
                                           # line (the "stop hit but never confirmed" alert always goes)


TIME_FIELDS = ("intraday_start", "intraday_end", "job_time", "reminder_time", "login_heads_up_time", "close_check_time",
               "top_setups_time", "morning_plan_time", "gap_check_time", "day_summary_time")


CHOICES = {"b_alerts": ("digest", "push", "off"), "correction_mode": ("caution", "mute_buys"),
           "alert_style": ("summary", "every")}


def check_alert_times(sec: "AlertsConfig") -> None:
    """HH:MM everywhere; login link times must be from 06:00 (an earlier login dies at 06:00); choice fields."""
    for f, allowed in CHOICES.items():
        if getattr(sec, f) not in allowed:
            raise ValueError(f"{f} must be one of {list(allowed)}")
    def parse(v):
        h, _, m = str(v).partition(":")
        if not (h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError(f"time {v!r} must be HH:MM")
        return int(h), int(m)
    for f in TIME_FIELDS:
        parse(getattr(sec, f))
    for t in sec.login_times:
        if parse(t) < (6, 0):
            raise ValueError(f"login time {t} is before 06:00: a session from then dies at 06:00")


def check_ai_times(sec: "AIConfig") -> None:
    """🤖 The quiet hours are HH:MM (24-hour inside; the pages show them 12-hour)."""
    for f in ("quiet_from", "quiet_to"):
        h, _, m = str(getattr(sec, f)).partition(":")
        if not (h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError(f"time {getattr(sec, f)!r} must be HH:MM")


OPTIONS_TIMES = ("window_start", "window_end", "time_exit", "reentry_until", "afternoon_time")


def check_options_times(sec: "OptionsConfig") -> None:
    """🧩 The options book's clock keys are HH:MM (24-hour inside; the pages show them 12-hour)."""
    for f in OPTIONS_TIMES:
        h, _, m = str(getattr(sec, f)).partition(":")
        if not (h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError(f"{f} {getattr(sec, f)!r} must be HH:MM")


@dataclass
class OptionsConfig:
    """🧩 NIFTY weekly options book (paper only): a credit spread sold on the opening-range bias, confirmed by the option
    chain. The Real switch exists but cannot be turned on (no webhook yet: the API answers 400 "not wired")."""
    capital: float = 300000.0              # the options book's own capital (deposits like the other books)
    capital_per_lot: float = 150000.0      # size: 1 lot per this much capital ...
    max_lots: int = 2                      # ... at most this many lots
    hedge_points: int = 250                # the bought leg this many points further out (200 when its premium < Rs 2)
    max_delta: float = 0.30                # the sold strike: the first with |delta| at most this
    min_premium: float = 20.0              # the sold premium must be at least this (else one strike nearer, delta 0.35 at most)
    otm_pct: float = 1.0                   # the sold strike: at least this % out of the money (50-strike grid)
    stop_pct: float = 100.0                # stop when the sold premium rises this % over the entry (100 = 2x) ...
    stop_pct_expiry: float = 70.0          # ... this on expiry day (70 = 1.7x)
    vwap_exit: bool = True                 # the VWAP exit: a 5-minute close back through VWAP against the bias closes the spread
    be_after_pct: float = 40.0             # after the sold premium has decayed this %, the stop becomes breakeven
    target_pct: float = 60.0               # target: the spread's value down this % from the entry credit
    time_exit: str = "15:10"               # the time exit (a hard exit at 15:20 if it failed; nothing is held overnight)
    window_start: str = "09:45"            # the bias is looked for on 5-minute closes from ...
    window_end: str = "11:00"              # ... to this; no break by then = no trade today
    max_trades_day: int = 3                # trades a day at most (re-entries and the afternoon pair count; the pair = 1)
    # 🧩 more entries (paper): each its own switch on the Options page
    reentry_same_bias: bool = True         # after a stop: once per bias, the SAME bias again on a close back over range + VWAP
    reentry_late: bool = True              # re-entries (same or flipped bias) until reentry_until instead of window_end
    reentry_until: str = "13:00"           # ... to here (first entries keep window_end)
    afternoon_sell: bool = True            # at afternoon_time on a quiet day: a call spread above + a put spread below
    afternoon_time: str = "13:30"          # when the afternoon pair is looked at (once a day, flat only)
    afternoon_max_range_pct: float = 0.6   # ... only when the day's range so far is at most this % of spot
    daily_loss_pct: float = 1.5            # no new entries once the day's realised + open loss reaches this % of capital
    vix_half: float = 20.0                 # India VIX over this halves the size (1 lot at least)
    pcr_bull: float = 1.1                  # chain score: PCR over this confirms a bullish bias ...
    pcr_bear: float = 0.9                  # ... under this a bearish one (over 1.5 / under 0.6 = crowded, size halves)
    max_gap_pct: float = 1.0               # no entry when the open gapped more than this % from the previous close
    margin_per_lot: float = 52000.0        # the margin estimate shown per lot
    event_days: str = ""                   # comma-separated ISO dates with no entries (RBI policy, budget, ...)
    paper: bool = True                     # the paper book runs
    real: bool = False                     # the Real switch: cannot be set (no webhook yet)
    enabled: bool = True                   # the enable switch (off: no new entries; an open spread still exits)


@dataclass
class AIConfig:
    """🤖 Local AI (an Ollama service next to the app): it reads and explains, it never decides or acts. Off until both
    models are present; a full stop (`ai.stopped` in app_settings) unloads the model and empties the queue."""
    enabled: bool = False                  # the master switch: nothing runs while off (turning on needs both models)
    night_batch: bool = True               # night work (the 7B model) from the evening job's end until night_end
    day_answers: bool = True               # day answers (the 3B model only) 9:00-15:40
    night_model: str = "qwen2.5:7b-instruct"   # ~4.7 GB; the evening / night batch
    day_model: str = "qwen2.5:3b-instruct"     # ~1.9 GB; quick answers while the market is open
    timeout_s: int = 180                   # one generate call may take at most this long (a long filing on 2.5 cores)
    keep_alive: str = "10m"                # how long a model stays loaded after a call (0 = unload at once)
    min_free_ram_gb: float = 4.0           # no AI work when the server's free RAM is under this
    slow_tick_s: float = 2.0               # ... or when the scheduler's last tick took longer than this
    breaker_failures: int = 3              # this many failures in a row open the breaker ...
    breaker_minutes: int = 60              # ... for this long; one success closes it again
    night_end: str = "06:00"               # night work stops here (HH:MM, 24-hour internally; shown 12-hour)
    max_queue: int = 200                   # the queue refuses new jobs beyond this many waiting
    notes_per_night: int = 80              # filing / deal notes a night at most (held names first, then plan, then watch)
    note_days: int = 3                     # how many days back the notes job reads filings and deals (the first run too)
    # 🤖 suggestions (PR 3 · A)
    max_suggestions_open: int = 6          # open suggestions at most (older ones expire on their own time)
    suggest_muted: str = ""                # comma list of suggestion kinds never born: skip, exit, flag, hold_options
    # 🤖 why (PR 3 · B)
    why: bool = True                       # the nightly "why" line under every trade closed today (and the evening message)
    # 🤖 brief
    brief: bool = True                     # the morning brief on trading days (one night-model call; a Telegram message)
    brief_time: str = "09:00"              # when the brief is written (HH:MM; the window for kind "brief" runs to 9:15 AM)
    # 🤖 ask
    ask: bool = True                       # "ask the app": the day model answers from one whitelisted read-only tool, any time
    # 🤖 AI view: a rating beside every system-suggested stock (the model rates, it never acts)
    view_enabled: bool = True              # the nightly rating of the plan, held, Follow and Screens names
    view_rate_held: bool = True            # held names re-rated nightly (hold / trim / exit-watch)
    view_rank_by_score: bool = False       # LOCKED until unlocked: plan ties (the same breakout rank) ordered by the AI score
    view_size_by_score: bool = False       # LOCKED until unlocked: BUY qty × clip(0.5 + 0.5 × (score − 40) / 40, floor, 1.0); never above 1.0×
    view_size_floor: float = 0.5           # the smallest size multiplier size_by_score may use (0.5-1.0)
    view_exit_watch_alert: bool = False    # LOCKED until unlocked: an alert when a held name closes under its fails_below (flag only)
    view_score_buy: int = 60               # score at or over this: buy (held: hold)
    view_score_avoid: int = 40             # score under this: avoid (held: exit-watch); between: wait (held: trim)
    view_max_per_night: int = 40           # names rated a night at most (plan, held, Follow, Screens, in that order)
    # 📖 the week's story: up to 3 observations about the closed week (one night-model call on Sunday evening)
    week: bool = True
    # 🤖 quiet hours: AI messages (notes, why, suggestions, ratings) are held from quiet_from to quiet_to and sent then
    # (or with the morning brief, whichever first). Never held: the brief, the Kite login, stops, orders. Equal = off.
    quiet_from: str = "23:00"
    quiet_to: str = "09:00"


TIPS_SIZE_MODES = ("lots", "rupees")


@dataclass
class TipsConfig:
    """💡 Tips: a Telegram channel's calls caught, decoded, tracked and scored. Standalone: not linked to any other book
    or rule. Nothing is automatic except the Tips paper book following the plan and the channel's own exits (and
    telling you). The reader logs in with your own Telegram account (TIPS_TG_* in .env, never here)."""
    enabled: bool = False                  # the reader runs (stays off until the reader is logged in)
    channels: str = "Index Options Buying with Aakanksha"   # one per line / semicolon: a username, or a private channel's title / id (commas split usernames only)
    capital: float = 100000.0              # the Tips paper book's own capital
    lots: int = 1                          # lots per taken hint (a size in the post wins; `take N 2` overrides)
    size_mode: str = "lots"                # lots | rupees (rupees: as many lots as `rupees` buys at the fill, 1 at least)
    rupees: float = 20000.0                # the premium per taken hint in rupees mode
    push_hints: bool = True                # every new hint pushed to Telegram at once
    push_followups: bool = True            # each channel follow-up pushed as one line
    auto_follow_channel_exit: bool = True  # the paper book follows the channel's own exit / SL hit / SL change (you are told)
    intraday_time_stop: str = "15:14"      # option buys: anything still open is closed here (HH:MM; shown 12-hour); 3:14 PM, ahead of the 3:15 broker square-off window
    partial_at_t1: bool = True             # half the position booked at target 1
    trail_to_cost_after_t1: bool = True    # after target 1 the stop moves to the entry price
    model_classify: bool = True            # the local 3B model sorts the posts code could not place (hint / follow-up / chatter)
    preview_fallback: bool = True          # the public preview page every 30 s while the reader is down
    # ⚡ Tips → Kite orders (real money, Kite Connect): off | dry (a rehearsal: the same buttons and legs, a simulator
    # fills from live quotes, nothing reaches Kite) | click (every leg by your click, each through a confirm; Place all =
    # entry now, the stop the moment it fills; only the stop rests on Kite, the targets are watched by the app) | auto
    # (entries armed on the level / the channel's activation, SL on the fill, targets watched, the channel's exits
    # followed, you are told)
    kite_mode: str = "off"
    kite_product: str = "NRML"             # NRML | MIS
    kite_market_protection: float = 5.0    # % market protection on MARKET / SL-M orders (the exchange rule for API orders: never 0)
    kite_sl_buffer_pct: float = 1.5        # stop orders go as SL (trigger + limit): the limit sits this % past the trigger (options allow no SL-M)
    kite_max_entries_day: int = 4          # entry orders a day at most
    kite_max_lots: int = 2                 # lots per entry order at most
    kite_loss_cap: float = 6000.0          # ₹ realised loss a day from these orders; at or past it no new entry
    kite_entry_from: str = "09:20"         # entries only from here (HH:MM; shown 12-hour)
    kite_entry_until: str = "14:30"        # ... until here
    kite_auto_max_open: int = 1            # auto: Kite hints open (armed or entered) at once; a second one waits
    kite_auto_book_part: str = "channel"   # auto, "book part" with ONE lot open: channel = book it | hold = keep it, stop to cost
    kite_reenter: str = "new call"         # a fresh call on a contract traded earlier today: "new call" = arm it once the earlier
                                           # position is closed | "never" = one entry per contract per day
    # 📏 Tips sizing (backend/tips/sizing.py): post = the post's size wins, else `lots` | fixed = always `lots` (the post's
    # size shown, ignored) | risk = floor(kite_risk_per_trade ÷ ((entry − stop) × lot size)), 1 at least. kite_max_lots
    # caps every mode; an entry whose loss at the stop is over kite_risk_per_trade is never armed / placed (every mode).
    kite_size_mode: str = "post"
    kite_risk_per_trade: float = 6000.0    # ₹ at the stop per trade: the risk mode's figure AND a guard in every mode
    kite_book_part: str = "half"           # "book part" / T1 with 2+ lots: half = floor(lots ÷ 2) lots | one = one lot
    kite_follow_adds: bool = False         # follow the channel's "add 2 lots near 660": ONE extra LIMIT after the fill (opt-in)
    # 📷 the channel's screenshots read on this server (RapidOCR): off = no download, no read. Images kept 2 days at most.
    vision: bool = False
    vision_mode: str = "report"            # report = a strike mismatch is shown and pushed | check = auto also holds / cancels
    # ↗ relay: every new channel post forwarded as-is from your own Telegram account (the reader's client) to these people
    relay: bool = False
    relay_to: str = ""                     # one per line / ';': @username, a phone in international form (+91…, in your contacts) or a numeric id
    # 🤖 AI on Tips (backend/tips/ai4.py): the local model writes sentences only, every number comes from code, nothing
    # here places, modifies, cancels, holds or blocks an order. Each off = nothing queued, no block, no line.
    ai_review: bool = True                 # the evening review of every hint with a Kite position today (3:35 PM / 6:35 PM)
    ai_grade: bool = True                  # the call grade A–D by code (the channel's record + the shape) and one [AI] read line
    ai_preflight: bool = False             # a pre-flight read of the post vs the decoded plan as a hint is armed (never delays the arm)
    ai_weekly: bool = True                 # the weekly channel profile, Sunday 7:05 PM (Research › Tips › Track)


TIPS_KITE_MODES = ("off", "dry", "click", "auto")
TIPS_KITE_PRODUCTS = ("NRML", "MIS")
TIPS_KITE_SIZE_MODES = ("post", "fixed", "risk")


def _hhmm_ok(v) -> bool:
    h, _, m = str(v).partition(":")
    return h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60


RELAY_USER_RE = re.compile(r"@?[A-Za-z][A-Za-z0-9_]{3,31}")


def split_relay(raw) -> list[str]:
    """tips.relay_to → entries, each in one form: '@username', '+<digits>' (a phone, international form) or '<digits>'
    (a numeric Telegram id). New lines and ';' separate. ValueError on an entry that is none of the three."""
    out = []
    for part in re.split(r"[;\n]+", str(raw or "")):
        part = part.strip()
        if not part:
            continue
        if part.startswith("+"):
            digits = re.sub(r"[\s().-]", "", part[1:])
            if not (digits.isdigit() and 8 <= len(digits) <= 15):
                raise ValueError(f"relay_to: {part!r} is not a phone in international form (+91…)")
            e = "+" + digits
        elif re.fullmatch(r"-?\d{5,20}", part):
            e = part
        elif RELAY_USER_RE.fullmatch(part):
            e = "@" + part.lstrip("@")
        else:
            raise ValueError(f"relay_to: {part!r} is not an @username, a phone (+91…) or a numeric id")
        if e.lower() not in (x.lower() for x in out):
            out.append(e)
    return out


def check_tips(sec: "TipsConfig") -> None:
    """💡 size_mode lots|rupees, the time stop HH:MM, sizes and capital positive; relay_to parsed and written one per
    line."""
    if sec.size_mode not in TIPS_SIZE_MODES:
        raise ValueError("size_mode must be lots or rupees")
    h, _, m = str(sec.intraday_time_stop).partition(":")
    if not (h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60):
        raise ValueError(f"intraday_time_stop {sec.intraday_time_stop!r} must be HH:MM")
    if int(sec.lots) < 1 or float(sec.capital) <= 0 or float(sec.rupees) <= 0:
        raise ValueError("lots, capital and rupees must be positive")
    if sec.kite_mode not in TIPS_KITE_MODES:
        raise ValueError("kite_mode must be off, dry, click or auto")
    if sec.kite_product not in TIPS_KITE_PRODUCTS:
        raise ValueError("kite_product must be NRML or MIS")
    if not (0.5 <= float(sec.kite_market_protection) <= 20):
        raise ValueError("kite_market_protection must be 0.5 … 20 (%), never 0 for API orders")
    if not (0.2 <= float(sec.kite_sl_buffer_pct) <= 10):
        raise ValueError("kite_sl_buffer_pct must be 0.2 … 10 (%)")
    for k in ("kite_entry_from", "kite_entry_until"):
        if not _hhmm_ok(getattr(sec, k)):
            raise ValueError(f"{k} {getattr(sec, k)!r} must be HH:MM")
    if str(sec.kite_entry_from).zfill(5) >= str(sec.kite_entry_until).zfill(5):
        raise ValueError("kite_entry_from must be before kite_entry_until")
    if int(sec.kite_max_entries_day) < 1 or int(sec.kite_max_lots) < 1 or float(sec.kite_loss_cap) <= 0:
        raise ValueError("kite_max_entries_day, kite_max_lots and kite_loss_cap must be positive")
    if int(sec.kite_auto_max_open) < 1:
        raise ValueError("kite_auto_max_open must be 1 or more")
    if sec.kite_auto_book_part not in ("channel", "hold"):
        raise ValueError("kite_auto_book_part must be channel or hold")
    if getattr(sec, "kite_reenter", "new call") not in ("new call", "never"):
        raise ValueError("kite_reenter must be 'new call' or 'never'")
    if getattr(sec, "vision_mode", "report") not in ("report", "check"):
        raise ValueError("vision_mode must be report or check")
    if sec.kite_size_mode not in TIPS_KITE_SIZE_MODES:
        raise ValueError("kite_size_mode must be post, fixed or risk")
    if sec.kite_book_part not in ("half", "one"):
        raise ValueError("kite_book_part must be half or one")
    if float(sec.kite_risk_per_trade) <= 0:
        raise ValueError("kite_risk_per_trade must be positive")
    from backend.tips import sizing                  # 📏 lots × lot size × the usual stop distance vs the risk / loss caps
    bad = sizing.check_settings(sec)
    if bad:
        raise ValueError(bad)
    sec.relay_to = "\n".join(split_relay(sec.relay_to))


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass
class MaintenanceConfig:
    """🧹 Archive and vacuum: once a week, rows older than `archive_days` in the fast-growing log tables MOVE to
    data/archive.db (never deleted), then the live DB is checkpointed and, with `vacuum`, compacted."""
    enabled: bool = True                   # the weekly archive job runs
    archive_days: int = 120                # rows older than this many days move to the archive file
    vacuum: bool = True                    # VACUUM the live DB after the move (gives the space back to the disk)
    day: str = "sunday"                    # the weekday the job runs
    time: str = "03:00"                    # HH:MM (IST) on that day; a missed run catches up later that day


def check_maintenance(sec: "MaintenanceConfig") -> None:
    """🧹 day is a weekday name, time HH:MM, archive_days at least 30 (the options replay reads up to 90 days)."""
    if str(sec.day).lower() not in WEEKDAYS:
        raise ValueError(f"day must be one of {list(WEEKDAYS)}")
    sec.day = str(sec.day).lower()
    h, _, m = str(sec.time).partition(":")
    if not (h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60):
        raise ValueError(f"time {sec.time!r} must be HH:MM")
    if int(sec.archive_days) < 30:
        raise ValueError("archive_days must be at least 30")


def check_market(sec: "MarketConfig") -> None:
    """📈 kronos_show shadow|cards, interval_s ≥ 30, horizon_min 15|30, samples 1-30, port 1024-65535."""
    if sec.kronos_show not in ("shadow", "cards"):
        raise ValueError("kronos_show must be shadow or cards")
    if int(sec.interval_s) < 30:
        raise ValueError("interval_s must be at least 30")
    if int(sec.horizon_min) not in (15, 30):
        raise ValueError("horizon_min must be 15 or 30")
    if not (1 <= int(sec.samples) <= 30):
        raise ValueError("samples must be 1 … 30")
    if not (1024 <= int(sec.port) <= 65535):
        raise ValueError("port must be 1024 … 65535")


def check_real(sec: "RealConfig") -> None:
    """💰 confirm_time is HH:MM or "" (off); tt_sid is a deployment id, 0 = not set."""
    v = str(sec.confirm_time or "")
    if v:
        h, _, m = v.partition(":")
        if not (h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError(f"confirm_time {v!r} must be HH:MM (or empty: off)")
    if int(sec.tt_sid) < 0:
        raise ValueError("tt_sid must be 0 (not set) or the deployment id")


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")   # 🤖 the local AI service (compose network; not a secret)

VARIANTS = ("E",)                      # the swing variants that exist: E+ only (A, the 200-day filter, removed Nov 2026)
# Keys of a saved section from before A was removed (E's own settings): read into the field they became, once.
LEGACY_KEYS = {"breakout": {"top_e": "top", "late_at_e": "late_at", "late_pct_e": "late_pct"}}

# Sections persisted to the DB and editable at runtime.
PERSISTED_SECTIONS = {"ingest": IngestConfig, "bases": BaseConfig, "screen": ScreenConfig, "trend": TrendConfig,
                      "vcp": VcpConfig, "flag": FlagConfig, "alerts": AlertsConfig,
                      "market": MarketConfig, "breakout": BreakoutConfig,
                      "paper": PaperConfig, "real": RealConfig,
                      "options": OptionsConfig, "ai": AIConfig,
                      "tips": TipsConfig, "maintenance": MaintenanceConfig}


@dataclass
class AppConfig:
    kite: KiteConfig = field(default_factory=KiteConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    bases: BaseConfig = field(default_factory=BaseConfig)
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    vcp: VcpConfig = field(default_factory=VcpConfig)
    flag: FlagConfig = field(default_factory=FlagConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    breakout: BreakoutConfig = field(default_factory=BreakoutConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    real: RealConfig = field(default_factory=RealConfig)
    options: OptionsConfig = field(default_factory=OptionsConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    tips: TipsConfig = field(default_factory=TipsConfig)
    maintenance: MaintenanceConfig = field(default_factory=MaintenanceConfig)
    server: ServerConfig = field(default_factory=ServerConfig)

    def section(self, name: str):
        if name not in PERSISTED_SECTIONS:
            raise KeyError(name)
        return getattr(self, name)

    def update_section(self, name: str, values: dict) -> dict:
        """Apply known keys with type coercion; unknown keys raise ValueError."""
        sec = self.section(name)
        before = replace(sec)
        known = {f.name: f for f in fields(sec)}
        unknown = set(values) - set(known)
        if unknown:
            raise ValueError(f"Unknown {name} keys: {sorted(unknown)}")
        for key, value in values.items():
            current = getattr(sec, key)
            if isinstance(current, bool):
                value = value.strip().lower() in ("1", "true", "yes", "on") if isinstance(value, str) else bool(value)
            elif isinstance(current, int):
                value = int(value)
            elif isinstance(current, float):
                value = float(value)
            elif isinstance(current, list):
                if not isinstance(value, list):
                    raise ValueError(f"{name}.{key} must be a list")
                kind = type(current[0]) if current else None
                if kind in (int, float):                   # UI sends list items as text
                    value = [kind(float(v)) if kind is int else float(v) for v in value]
            elif isinstance(current, str):
                value = str(value)
            setattr(sec, key, value)
        if name in ("alerts", "ai", "options", "tips", "maintenance", "real", "market"):
            try:
                {"alerts": check_alert_times, "ai": check_ai_times, "options": check_options_times, "tips": check_tips,
                 "maintenance": check_maintenance, "real": check_real, "market": check_market}[name](sec)
            except ValueError:
                for key in values:                       # roll back this update
                    setattr(sec, key, getattr(before, key))
                raise
        return asdict(sec)


def save_config(config: AppConfig) -> None:
    from backend.database.models import get_db, set_setting
    with get_db() as conn:
        for name in PERSISTED_SECTIONS:
            set_setting(conn, f"config.{name}", json.dumps(asdict(config.section(name))))


def load_config(config: AppConfig | None = None) -> AppConfig:
    """Overlay persisted sections onto defaults; stale keys are ignored, and so are saved sections that no longer
    exist (config.flow / config.futures from before the Intraday Flow / Futures removal stay in app_settings, unread)."""
    from backend.database.models import get_db, get_setting, set_setting
    config = config or AppConfig()
    with get_db() as conn:
        for name in PERSISTED_SECTIONS:
            raw = get_setting(conn, f"config.{name}")
            if not raw:
                continue
            sec = config.section(name)
            known = {f.name for f in fields(sec)}
            saved = json.loads(raw)
            for key, value in saved.items():
                if key in known:
                    setattr(sec, key, value)
            legacy = {k: v for k, v in LEGACY_KEYS.get(name, {}).items() if k in saved}
            if legacy:                                      # E's own keys from before A was removed: now the only ones
                for old_key, new_key in legacy.items():
                    setattr(sec, new_key, saved[old_key])
                set_setting(conn, f"config.{name}", json.dumps(asdict(sec)))
        if [v for v in config.paper.variants if v not in VARIANTS] or not config.paper.variants:
            config.paper.variants = [v for v in config.paper.variants if v in VARIANTS] or list(VARIANTS)
            set_setting(conn, "config.paper", json.dumps(asdict(config.paper)))
        if config.real.variant not in VARIANTS:
            config.real.variant = VARIANTS[0]
            set_setting(conn, "config.real", json.dumps(asdict(config.real)))
        if "cup" in config.alerts.screens:                      # the Cup & handle screen was removed
            config.alerts.screens = [x for x in config.alerts.screens if x != "cup"]
            set_setting(conn, "config.alerts", json.dumps(asdict(config.alerts)))
        if not get_setting(conn, "migrated.alerts_flag"):       # once: the bull flag screen, after VCP
            screens = config.alerts.screens
            if "vcp" in screens and "flag" not in screens:
                screens.insert(screens.index("vcp") + 1, "flag")
                set_setting(conn, "config.alerts", json.dumps(asdict(config.alerts)))
            set_setting(conn, "migrated.alerts_flag", "1")
        if not get_setting(conn, "migrated.options_entries"):  # once: 🧩 more entries: the old default of 2 trades a day -> 3
            if get_setting(conn, "config.options") and config.options.max_trades_day == 2:
                config.options.max_trades_day = 3
                set_setting(conn, "config.options", json.dumps(asdict(config.options)))
            set_setting(conn, "migrated.options_entries", "1")
    return config
