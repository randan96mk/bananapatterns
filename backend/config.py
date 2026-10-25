"""Central configuration.

Secrets (Kite credentials) come from .env and are never persisted.
Tunable sections are dataclasses saved to the `app_settings` table as JSON
and editable at runtime through /api/config/{section}. Phase 2 adds a `vcp`
section here for the screener dials using the same mechanism.
"""

import json
import os
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
    host: str = "127.0.0.1"
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
class CupConfig:
    """Cup with handle (O'Neil). Bars are trading sessions; depths are fractions."""
    min_prior_advance: float = 0.30        # rise into the left lip ...
    prior_lookback_bars: int = 130         # ... from the lowest low of this many bars before it
    min_cup_bars: int = 35                 # 7 weeks, left lip to right lip
    max_cup_bars: int = 325                # 65 weeks
    min_depth: float = 0.12                # left lip to cup bottom
    max_depth: float = 0.33
    right_lip_within: float = 0.10         # right lip at least (1 - this) x the left lip ...
    right_lip_above: float = 0.05          # ... and at most (1 + this) x it
    u_low_band: float = 0.05               # U-shape: sessions with a low within this of the cup low ...
    u_min_bars: int = 5                    # ... at least this many (a V bottom has one or two)
    u_bottom_frac: float = 0.25            # ... and at least this share of closes in the cup's bottom third
                                           #     (a straight-line V has ~0.33, a spike bottom ~0.1, a round U 0.4+)
    handle_min_bars: int = 5               # 1 week
    handle_max_bars: int = 25              # ~5 weeks
    handle_min_depth: float = 0.02
    handle_max_depth: float = 0.12
    handle_upper_half: bool = True         # handle low above the cup's midpoint
    handle_max_volume: float = 1.0         # handle volume / 50-day average before it (dry-up)
    max_dist_to_pivot: float = 0.05        # Forming: close within this under the handle high
    stop_buffer: float = 0.01              # stop = handle low - 1%, never more than vcp.max_stop under the pivot
    min_rs: int = 70
    min_trend_checks: int = 5
    history_bars: int = 400                # how far back breakouts are looked for (stages need ~6 months)


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
    screens: list = field(default_factory=lambda: ["vcp", "cup"])
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
    # Position guard (Kite holdings, read-only): sell-rule alerts after the evening scan
    position_guard: bool = True
    alert_buttons: bool = True             # Watch / Mute / Chart / I bought under stock alerts
    mute_days: int = 30
    profit_zone_pct: float = 20.0          # gain from your average: consider selling part
    extended_pct: float = 25.0             # close this far above the 50-day line: extended
    failed_days: int = 3                   # back under the pivot within N sessions of buying: failed breakout
    # Results calendar (NSE board meetings, fetched in the evening job): gap risk around results
    results_calendar: bool = True          # fetch the calendar and show "Results ..." on cards, alerts and plan
    results_warn_sessions: int = 5         # stock alerts / plan / cards warn when results are this close
    results_guard_sessions: int = 1        # guard: warn on a held stock this many sessions before its results


TIME_FIELDS = ("intraday_start", "intraday_end", "job_time", "reminder_time", "login_heads_up_time", "close_check_time",
               "top_setups_time", "morning_plan_time", "gap_check_time")


CHOICES = {"b_alerts": ("digest", "push", "off"), "correction_mode": ("caution", "mute_buys")}


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


# Sections persisted to the DB and editable at runtime.
PERSISTED_SECTIONS = {"ingest": IngestConfig, "bases": BaseConfig, "screen": ScreenConfig, "trend": TrendConfig,
                      "vcp": VcpConfig, "cup": CupConfig, "alerts": AlertsConfig, "market": MarketConfig}


@dataclass
class AppConfig:
    kite: KiteConfig = field(default_factory=KiteConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    bases: BaseConfig = field(default_factory=BaseConfig)
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    vcp: VcpConfig = field(default_factory=VcpConfig)
    cup: CupConfig = field(default_factory=CupConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
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
        if name == "alerts":
            try:
                check_alert_times(sec)
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
    """Overlay persisted sections onto defaults; stale keys are ignored."""
    from backend.database.models import get_db, get_setting, set_setting
    config = config or AppConfig()
    with get_db() as conn:
        for name in PERSISTED_SECTIONS:
            raw = get_setting(conn, f"config.{name}")
            if not raw:
                continue
            sec = config.section(name)
            known = {f.name for f in fields(sec)}
            for key, value in json.loads(raw).items():
                if key in known:
                    setattr(sec, key, value)
        # once: alert screens saved before the cup screen existed get it next to VCP
        if not get_setting(conn, "migrated.alerts_cup"):
            screens = config.alerts.screens
            if "vcp" in screens and "cup" not in screens:
                screens.insert(screens.index("vcp") + 1, "cup")
                set_setting(conn, "config.alerts", json.dumps(asdict(config.alerts)))
            set_setting(conn, "migrated.alerts_cup", "1")
    return config
