"""₿ Derivatives from Delta Exchange India's public ticker (no key): open interest, funding, mark price.

  GET https://api.india.delta.exchange/v2/tickers/BTCUSD -> {"result": {oi, oi_value_usd, funding_rate (%), mark_price, ...}}

One GET per closed candle, in the feed thread right after the candle poll (feed.CryptoService.tick), through
DeltaClient.get (the tests' fake Delta answers it). Stored in crypto_ticks(symbol, t = the closed candle's open, oi,
funding, mark), never deleted. A failed poll is logged and the step skipped: nothing waits, nothing retries; the
detections of that candle carry the last tick stored (or none).

Derived by code: oi_30m_pct / oi_24h_pct (the OI against the tick 30 min / 24 h before), funding (the 8 h rate, %),
funding_state ("neutral" | "stretched long" | "stretched short": |funding| ≥ FUNDING_STRETCHED %), and for a detection
oi_state ("flushed": oi_30m ≤ −OI_MOVE % · "building": ≥ +OI_MOVE % · "flat"). Alerts only: this reads prices."""

import time as _time
from datetime import datetime
from typing import Optional

from backend.config import IST
from backend.database.models import get_db
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm

logger = setup_logger("crypto")

URL = "https://api.india.delta.exchange/v2/tickers/{symbol}"
TIMEOUT_S = 3.0                    # a slow ticker never holds the candle step for long
FUNDING_STRETCHED = 0.03           # % per 8 h
OI_MOVE = 0.3                      # % in 30 min
STALE_S = 1800                     # a tick older than this is not "now"


def _f(v) -> Optional[float]:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def parse(body) -> dict:
    """Delta's ticker reply -> {oi, funding, mark}; raises ValueError without a result."""
    res = body.get("result") if isinstance(body, dict) else None
    if not isinstance(res, dict):
        raise ValueError(f"unexpected ticker reply: {str(body)[:120]}")
    oi = _f(res.get("oi"))
    if oi is None:
        oi = _f(res.get("oi_value_usd")) or _f(res.get("oi_value"))
    return {"oi": oi, "funding": _f(res.get("funding_rate")), "mark": _f(res.get("mark_price"))}


def store(symbol: str, t: int, tick: dict) -> None:
    with get_db() as conn:
        conn.execute("INSERT INTO crypto_ticks (symbol, t, oi, funding, mark) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(symbol, t) DO UPDATE SET oi=excluded.oi, funding=excluded.funding, mark=excluded.mark",
                     (symbol, int(t), tick.get("oi"), tick.get("funding"), tick.get("mark")))


def poll(client, symbol: str, t: int) -> Optional[dict]:
    """One ticker GET for the closed candle at `t`, stored; None (logged) when it fails. Never raises."""
    try:
        body = client.get(URL.format(symbol=symbol), {}, timeout=TIMEOUT_S)
        tick = parse(body)
        store(symbol, t, tick)
        return {"t": int(t), **tick}
    except Exception as exc:
        logger.warning("₿ Delta ticker poll failed (skipped): %s", f"{type(exc).__name__}: {str(exc)[:160]}")
        return None


def _tick(conn, symbol: str, at: int, not_before: Optional[int] = None) -> Optional[dict]:
    q, a = "SELECT t, oi, funding, mark FROM crypto_ticks WHERE symbol = ? AND t <= ?", [symbol, int(at)]
    if not_before is not None:
        q += " AND t >= ?"
        a.append(int(not_before))
    r = conn.execute(q + " ORDER BY t DESC LIMIT 1", a).fetchone()
    return dict(r) if r else None


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or not b:
        return None
    return round(100 * (a / b - 1), 2)


def funding_state(funding: Optional[float]) -> Optional[str]:
    if funding is None:
        return None
    if funding >= FUNDING_STRETCHED:
        return "stretched long"
    if funding <= -FUNDING_STRETCHED:
        return "stretched short"
    return "neutral"


def oi_state(oi_30m_pct: Optional[float]) -> Optional[str]:
    if oi_30m_pct is None:
        return None
    if oi_30m_pct <= -OI_MOVE:
        return "flushed"
    if oi_30m_pct >= OI_MOVE:
        return "building"
    return "flat"


def derived(symbol: str, t: int) -> Optional[dict]:
    """The tick at or before `t` (within STALE_S) and its changes: {t, oi, funding, funding_8h, mark, oi_30m_pct,
    oi_24h_pct, funding_state, oi_state} or None."""
    with get_db() as conn:
        cur = _tick(conn, symbol, t, int(t) - STALE_S)
        if not cur:
            return None
        p30 = _tick(conn, symbol, cur["t"] - 1800, cur["t"] - 1800 - 900)
        p24 = _tick(conn, symbol, cur["t"] - 86400, cur["t"] - 86400 - 7200)
    o30 = _pct(cur["oi"], (p30 or {}).get("oi"))
    o24 = _pct(cur["oi"], (p24 or {}).get("oi"))
    return {"t": cur["t"], "oi": cur["oi"], "funding": cur["funding"], "funding_8h": cur["funding"], "mark": cur["mark"], "oi_30m_pct": o30,
            "oi_24h_pct": o24, "funding_state": funding_state(cur["funding"]), "oi_state": oi_state(o30)}


def for_detection(symbol: str, t: int) -> Optional[dict]:
    """derivs_json for a detection on the candle at `t`: {oi_30m_pct, oi_24h_pct, funding, oi_state, funding_state}."""
    d = derived(symbol, t)
    if not d:
        return None
    return {k: d[k] for k in ("oi_30m_pct", "oi_24h_pct", "funding", "oi_state", "funding_state")}


# --- the words -------------------------------------------------------------------------------------------------------------

def sp(v: Optional[float], nd: int = 1) -> str:
    """+1.8 % / −0.6 % (a real minus)."""
    if v is None:
        return "—"
    return f"{v:+.{nd}f} %".replace("-", "−")


def fund(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:+.3f} %".replace("-", "−")


def chip(d: Optional[dict]) -> Optional[str]:
    """'OI 24h +1.8 % · last 30 min −0.6 % · funding +0.012 % (8 h) · neutral'."""
    if not d:
        return None
    return f"OI 24h {sp(d.get('oi_24h_pct'))} · last 30 min {sp(d.get('oi_30m_pct'))} · funding {fund(d.get('funding'))} (8 h) · {d.get('funding_state') or '—'}"


def steps(dj: Optional[dict], direction: str) -> dict:
    """The grade's derivative steps: {oi: +1 flushed into the sweep / −1 building against it / 0, funding: +1 stretched
    against the trade (longs into crowded shorts, shorts into crowded longs) / 0}."""
    if not dj:
        return {"oi": 0, "funding": 0}
    oi = {"flushed": 1, "building": -1}.get(dj.get("oi_state"), 0)
    against = "stretched short" if direction == "long" else "stretched long"
    return {"oi": oi, "funding": 1 if dj.get("funding_state") == against else 0}


def row_view(dj: Optional[dict], direction: str) -> Optional[dict]:
    """The detections table's chip: {chip 'OI −0.6 % · fund neutral' | 'OI +0.9 % against', oi_state, funding_state, tone}."""
    if not dj:
        return None
    st = steps(dj, direction)
    oi = dj.get("oi_30m_pct")
    if st["oi"] < 0:
        text, tone = f"OI {sp(oi)} against", "warn"
    else:
        text = f"OI {sp(oi)} · fund {dj.get('funding_state') or '—'}"
        tone = "ok" if (st["oi"] > 0 or st["funding"] > 0) else "dim"
    return {"chip": text, "oi_state": dj.get("oi_state"), "funding_state": dj.get("funding_state"), "tone": tone}


def alert_line(dj: Optional[dict], direction: str, session_label: Optional[str], session_ok: bool = True) -> str:
    """'derivatives (Delta): OI −0.6 % in 30 min (longs flushed) ✓ · funding +0.012 % neutral · session New York ✓'."""
    sess = f"session {session_label} {'✓' if session_ok else '✗'}" if session_label else None
    if not dj:
        return " · ".join(x for x in ("derivatives (Delta): no ticker", sess) if x)
    st = steps(dj, direction)
    who = "longs" if direction == "long" else "shorts"
    oi_word = {"flushed": f"({who} flushed) ✓", "building": "(building against) ✗", "flat": "(flat)"}.get(dj.get("oi_state"), "")
    fs = dj.get("funding_state") or "—"
    bits = [f"derivatives (Delta): OI {sp(dj.get('oi_30m_pct'))} in 30 min {oi_word}".rstrip(),
            f"funding {fund(dj.get('funding'))} {fs}" + (" ✓" if st["funding"] > 0 else "")]
    if sess:
        bits.append(sess)
    return " · ".join(bits)


def now(config, now_ts: Optional[float] = None) -> dict:
    """The summary's derivs_now: {on, oi, oi_24h_pct, oi_30m_pct, funding (%), funding_state, oi_state, mark, chip, at_label, tone} (empty
    values when the ticker is off or nothing is stored yet)."""
    c = config.crypto
    now_ts = _time.time() if now_ts is None else now_ts
    d = derived(c.symbol, int(now_ts)) if c.derivs else None
    base = {"on": bool(c.derivs), "oi": None, "oi_24h_pct": None, "oi_30m_pct": None, "funding": None, "funding_state": None, "mark": None,
            "oi_state": None, "chip": None, "at_label": None, "tone": "dim"}
    if not d:
        return base
    return {**base, **{k: d[k] for k in ("oi", "oi_24h_pct", "oi_30m_pct", "funding", "funding_state", "mark", "oi_state")}, "chip": chip(d),
            "at": d["t"], "at_label": ampm(datetime.fromtimestamp(d["t"], IST)), "tone": now_tone(d)}


def now_tone(d: Optional[dict]) -> str:
    """The header chip's tone: warn when funding is stretched or OI is building, ok when OI was flushed, else dim."""
    if not d:
        return "dim"
    if (d.get("funding_state") or "neutral") != "neutral" or d.get("oi_state") == "building":
        return "warn"
    return "ok" if d.get("oi_state") == "flushed" else "dim"
