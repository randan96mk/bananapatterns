"""Turn a finished scan into Telegram alerts, each sent at most once.

Dedup keys (sent_alerts.key):
  stage move   {as_of}|{screen}|{symbol}|{kind}
  near pivot   near|{screen}|{symbol}|{pivot}        once per base, not daily
  digest       {as_of}|{screen}|digest|{chunk}
  intraday     {date}|intraday|{symbol}
  system       caller-provided (e.g. {date}|login-reminder)
A failed send is recorded with ok=0 and retried on the next dispatch.
"""

import json
import os
from typing import Optional

from backend.alerts import messages as msg
from backend.alerts.charts import render_chart
from backend.alerts.telegram import TelegramClient, TelegramError
from backend.config import AlertsConfig, BaseConfig
from backend.database.models import get_candles, get_db, now_ist
from backend.engine.bases import detect_bases
from backend.logging_config.logger import setup_logger

logger = setup_logger("alerts")

# feed kind -> AlertsConfig toggle ("climbing" in the feed = reclaimed the pivot after falling under it)
KIND_TOGGLE = {"breakout": "breakout", "breakout_low_volume": "low_volume_breakout", "below_pivot": "below_pivot",
               "played_out": "played_out", "forming": "forming", "left": "left", "climbing": "below_pivot"}
CHART_KINDS = {"breakout", "breakout_low_volume", "near_pivot"}


class Notifier:
    """Sends through Telegram and records every attempt in sent_alerts."""

    def __init__(self, cfg: AlertsConfig, client: Optional[TelegramClient] = None):
        self.cfg = cfg
        self.client = client or TelegramClient()
        self.chat_id = (cfg.chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()

    @property
    def ready(self) -> bool:
        return bool(self.cfg.enabled and self.client.configured and self.chat_id)

    def already_sent(self, key: str) -> bool:
        with get_db() as conn:
            row = conn.execute("SELECT ok FROM sent_alerts WHERE key = ?", (key,)).fetchone()
        return bool(row and row["ok"])

    def send(self, key: str, kind: str, html: str, symbol: Optional[str] = None, png: Optional[bytes] = None) -> bool:
        if not self.ready or self.already_sent(key):
            return False
        ok, err = True, None
        try:
            if png:
                self.client.send_photo(self.chat_id, png, html)
            else:
                self.client.send_message(self.chat_id, html)
        except TelegramError as exc:
            ok, err = False, str(exc)
            logger.error("Telegram send failed (%s): %s", kind, exc)
        with get_db() as conn:
            conn.execute("INSERT INTO sent_alerts (key, sent_at, kind, symbol, text, ok, error) VALUES (?,?,?,?,?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET sent_at=excluded.sent_at, ok=excluded.ok, error=excluded.error",
                         (key, now_ist(), kind, symbol, html, int(ok), err))
        return ok

    def system(self, key: str, text: str) -> bool:
        return self.cfg.system and self.send(key, "system", msg.system_alert(text))


def _chart_png(candles: list[dict], p: dict, kind: str, bases_cfg: BaseConfig) -> Optional[bytes]:
    try:
        bases = detect_bases(candles, bases_cfg)
        if not bases:
            return None
        if kind == "near_pivot":
            base, bo_date = bases[-1], None
        else:
            base = next((b for b in reversed(bases) if b.breakout is not None), bases[-1])
            bo_date = base.breakout.date if base.breakout else None
        v = p.get("vcp") or {}
        title = f"{p['symbol']} · {p.get('company') or ''} — {p.get('base_label') or base.label}"
        sub = " · ".join(x for x in (f"RS {p['rs']}" if p.get("rs") is not None else "",
                                     f"Trend {p.get('tt_passed')}/{p.get('tt_of')}" if p.get("tt_of") else "",
                                     p.get("industry") or "") if x)
        return render_chart(candles, title, sub, base={"start_date": base.start_date, "end_date": base.end_date,
                                                       "pivot": base.pivot, "low": base.low, "label": base.label},
                            stop=v.get("stop"), breakout_date=bo_date, contractions=v.get("contractions"))
    except Exception:
        logger.exception("Chart render failed for %s", p.get("symbol"))
        return None


def dispatch_scan(as_of: str, cfg: AlertsConfig, bases_cfg: BaseConfig, notifier: Notifier,
                  tokens: dict[str, int], fresh_sessions: int = 5) -> dict:
    """Send alerts for one as-of date. tokens: symbol -> instrument_token (for charts)."""
    sent = {"stage": 0, "near_pivot": 0, "digest": 0, "skipped_ready": not notifier.ready}
    if not notifier.ready:
        logger.info("Alerts not sent: Telegram not configured or alerts disabled")
        return sent
    with get_db() as conn:
        run = conn.execute("SELECT counts FROM scan_runs WHERE as_of = ? ORDER BY id DESC LIMIT 1", (as_of,)).fetchone()
        counts_all = json.loads(run["counts"]) if run else {}
    for screen in cfg.screens:
        with get_db() as conn:
            payloads = {r["symbol"]: dict(json.loads(r["payload"]), stage=r["stage"]) for r in conn.execute(
                "SELECT symbol, stage, payload FROM scan_results WHERE as_of = ? AND screen = ?", (as_of, screen))}
            moves = [dict(r) for r in conn.execute(
                "SELECT symbol, kind, text, change_pct, close FROM scan_changes WHERE as_of = ? AND screen = ? "
                "ORDER BY CASE kind WHEN 'breakout' THEN 0 WHEN 'below_pivot' THEN 1 WHEN 'played_out' THEN 2 "
                "WHEN 'forming' THEN 3 ELSE 4 END, symbol", (as_of, screen))]

        def candles_of(sym):
            tok = tokens.get(sym)
            if tok is None:
                return []
            with get_db() as conn:
                return get_candles(conn, tok)

        for m in moves:
            p = payloads.get(m["symbol"])
            kind = m["kind"]
            if kind == "breakout" and p and p.get("stage") == "fresh_low_volume":
                kind = "breakout_low_volume"
            toggle = KIND_TOGGLE.get(kind)
            if not toggle or not getattr(cfg, toggle, False):
                continue
            cir, png = None, None
            if kind in CHART_KINDS and p:
                cs = candles_of(m["symbol"])
                if cs:
                    last = cs[-1]
                    rng = last["high"] - last["low"]
                    cir = (last["close"] - last["low"]) / rng if rng > 0 else None
                    if cfg.chart_images:
                        png = _chart_png(cs, p, kind, bases_cfg)
            html = msg.stage_alert(kind, screen, m["text"], p or {"symbol": m["symbol"]}, cir)
            if notifier.send(f"{as_of}|{screen}|{m['symbol']}|{kind}", kind, html, m["symbol"], png):
                sent["stage"] += 1

        if cfg.near_pivot:
            for sym, p in payloads.items():
                if p.get("stage") != "forming" or p.get("dist_to_pivot_pct") is None:
                    continue
                if p["dist_to_pivot_pct"] > cfg.near_pivot_pct * 100:
                    continue
                png = None
                if cfg.chart_images:
                    cs = candles_of(sym)
                    png = _chart_png(cs, p, "near_pivot", bases_cfg) if cs else None
                if notifier.send(f"near|{screen}|{sym}|{p['pivot']}", "near_pivot", msg.near_pivot_alert(screen, p),
                                 sym, png):
                    sent["near_pivot"] += 1

        if cfg.digest:
            for i, chunk in enumerate(msg.digest(screen, as_of, counts_all.get(screen, {}), moves, fresh_sessions)):
                if notifier.send(f"{as_of}|{screen}|digest|{i}", "digest", chunk):
                    sent["digest"] += 1
    logger.info("Alerts for %s: %s", as_of, sent)
    return sent
