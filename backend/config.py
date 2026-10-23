"""Central configuration.

Secrets (Kite credentials) come from .env and are never persisted.
Tunable sections are dataclasses saved to the `app_settings` table as JSON
and editable at runtime through /api/config/{section}. Phase 2 adds a `vcp`
section here for the screener dials using the same mechanism.
"""

import json
import os
from dataclasses import asdict, dataclass, field, fields
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
    min_base_bars: int = 15                # 3 weeks minimum (site shows 3w bases)
    max_base_bars: int = 325               # 65 weeks; longer ranges belong to the multi-year screen (later)
    max_base_depth: float = 0.40           # (pivot - base low) / pivot; deeper = correction, not a base
    max_pivot_drift: float = 0.03          # wicks may lift the pivot this far above the base's first high;
                                           # beyond it the stock is grinding up (a trend, not a base)
    prior_advance_bars: int = 63           # look back this far for the advance into the base
    min_prior_advance: float = 0.20        # base high >= 20% above the lowest low of that lookback
    reset_ma: int = 200                    # a close below this SMA restarts the base count at B1 (0 = off)
    reset_on_undercut: bool = True         # a base low under the previous base low restarts the count
    vol_baseline: str = "sma50"            # breakout volume vs "sma50" (brief) or "median20" (site's card)
    bo_vol_mult: float = 1.5               # volume_ok when breakout volume >= mult x baseline
    weak_close_cir: float = 0.5            # close-in-range below this = weak close
    squat_bars: int = 5                    # a close back under the pivot within N bars = squat
    dryup_lookback: int = 50               # bars before the base used for the volume dry-up ratio


# Sections persisted to the DB and editable at runtime.
PERSISTED_SECTIONS = {"ingest": IngestConfig, "bases": BaseConfig}


@dataclass
class AppConfig:
    kite: KiteConfig = field(default_factory=KiteConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    bases: BaseConfig = field(default_factory=BaseConfig)
    server: ServerConfig = field(default_factory=ServerConfig)

    def section(self, name: str):
        if name not in PERSISTED_SECTIONS:
            raise KeyError(name)
        return getattr(self, name)

    def update_section(self, name: str, values: dict) -> dict:
        """Apply known keys with type coercion; unknown keys raise ValueError."""
        sec = self.section(name)
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
            elif isinstance(current, list) and not isinstance(value, list):
                raise ValueError(f"{name}.{key} must be a list")
            elif isinstance(current, str):
                value = str(value)
            setattr(sec, key, value)
        return asdict(sec)


def save_config(config: AppConfig) -> None:
    from backend.database.models import get_db, set_setting
    with get_db() as conn:
        for name in PERSISTED_SECTIONS:
            set_setting(conn, f"config.{name}", json.dumps(asdict(config.section(name))))


def load_config(config: AppConfig | None = None) -> AppConfig:
    """Overlay persisted sections onto defaults; stale keys are ignored."""
    from backend.database.models import get_db, get_setting
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
    return config
