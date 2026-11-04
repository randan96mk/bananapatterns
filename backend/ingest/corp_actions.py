"""Splits and bonuses found in the daily candles themselves: a close that jumps 40% or more from the day before
while the turnover (price × volume) stays in line is a corporate action, not a move (a crash comes with a turnover
spike). Each one is kept in `corp_actions` as (date, ratio = open / previous close); `get_candles` scales every
earlier bar by the ratio, so every screen, backtest and tracker sees a continuous series. Checked after every
candle sync; nothing is fetched from outside."""

from statistics import median

from backend.database.models import get_candles, get_db
from backend.logging_config.logger import setup_logger

logger = setup_logger("corp_actions")

JUMP = 0.4                                   # a close this far from the day before
TURNOVER_BAND = (0.25, 4.0)                  # ...with the day's turnover within this of the median before it


def detect(candles: list[dict]) -> list[tuple[str, float]]:
    """[(date, ratio)] of the splits / bonuses in a raw (unadjusted) daily series."""
    out = []
    for i in range(1, len(candles)):
        a, b = candles[i - 1], candles[i]
        if not a["close"] or (1 - JUMP) < b["close"] / a["close"] < (1 + JUMP):
            continue
        prior = [c["close"] * c["volume"] for c in candles[max(0, i - 20):i] if c["volume"]]
        tm = median(prior) if prior else 0
        t = b["close"] * b["volume"]
        if tm and not TURNOVER_BAND[0] <= t / tm <= TURNOVER_BAND[1]:
            continue                                      # a real move on unusual turnover
        if i + 1 < len(candles) and (1 - JUMP) < candles[i + 1]["close"] / a["close"] < (1 + JUMP):
            continue                                      # one-day spike, back the next day: bad data, not a split
        out.append((b["date"], round(b["open"] / a["close"], 6) if b["open"] else round(b["close"] / a["close"], 6)))
    return out


def refresh(tokens: list[int]) -> int:
    """Re-detects for these instruments and replaces their rows. Returns how many actions are on file."""
    n = 0
    with get_db() as conn:
        for tok in tokens:
            acts = detect(get_candles(conn, tok, adjusted=False))
            conn.execute("DELETE FROM corp_actions WHERE instrument_token = ?", (tok,))
            conn.executemany("INSERT INTO corp_actions (instrument_token, date, ratio) VALUES (?, ?, ?)",
                             [(tok, d, r) for d, r in acts])
            n += len(acts)
    return n


def listing(symbols: dict) -> list[dict]:
    """Every action on file, newest first: [{symbol, date, ratio}] (symbols: token -> symbol)."""
    with get_db() as conn:
        rows = conn.execute("SELECT instrument_token, date, ratio FROM corp_actions ORDER BY date DESC").fetchall()
    return [{"symbol": symbols.get(r[0], str(r[0])), "date": r[1], "ratio": r[2]} for r in rows]
