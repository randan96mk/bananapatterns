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
import re
from html import escape, unescape
from typing import Optional

from backend.alerts import messages as msg
from backend.alerts import charts
from backend.alerts import intraday as itd
from backend.alerts.telegram import MAX_CAPTION, TelegramClient, TelegramError
from backend.config import AlertsConfig, BaseConfig, VcpConfig
from backend.database.models import get_candles, get_db, now_ist
from backend.engine.bases import detect_bases
from backend.logging_config.logger import setup_logger

logger = setup_logger("alerts")

# feed kind -> AlertsConfig toggle ("climbing" in the feed = reclaimed the pivot after falling under it)
KIND_TOGGLE = {"breakout": "breakout", "breakout_low_volume": "low_volume_breakout", "below_pivot": "below_pivot",
               "played_out": "played_out", "forming": "forming", "left": "left", "climbing": "below_pivot"}


ACTION_KINDS = {"breakout", "breakout_low_volume", "forming", "near_pivot", "intraday", "close_check", "plan",
                "climbing"}


def action_buttons(symbol: str) -> list:
    """Watch / Mute / Chart / I bought, answered by backend.alerts.commands."""
    return [("📌 Watch", None, f"w:{symbol}"), ("🔕 Mute", None, f"m:{symbol}"), ("📈 Chart", None, f"c:{symbol}"),
            ("✅ I bought", None, f"b:{symbol}")]


def strip_tags(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html))


def split_caption(html: str, limit: int = MAX_CAPTION) -> tuple[str, str]:
    """Split at a line break so the photo caption fits Telegram's limit; our tags never span lines."""
    if len(html) <= limit:
        return html, ""
    lines, cap = html.split("\n"), []
    while lines and len("\n".join(cap + [lines[0]])) <= limit:
        cap.append(lines.pop(0))
    if not cap:                                            # one huge first line: plain cut, tags stripped
        return escape(strip_tags(lines[0]))[:limit - 1] + "…", "\n".join(lines[1:])
    return "\n".join(cap), "\n".join(lines).strip("\n")


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

    def send(self, key: str, kind: str, html: str, symbol: Optional[str] = None, png: Optional[bytes] = None,
             buttons: Optional[list[tuple[str, str]]] = None) -> bool:
        if not self.ready or self.already_sent(key):
            return False
        ok, err = True, None
        try:
            if buttons is None and symbol and kind in ACTION_KINDS and self.cfg.alert_buttons:
                buttons = action_buttons(symbol)
            if png:
                caption, rest = split_caption(html)
                if buttons:
                    self.client.send_photo(self.chat_id, png, caption, buttons)
                else:
                    self.client.send_photo(self.chat_id, png, caption)
                if rest:
                    self.client.send_message(self.chat_id, rest)
            elif buttons:
                self.client.send_message(self.chat_id, html, buttons)
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


_warned_no_charts = False


def charts_available() -> bool:
    """False (with one log warning) when matplotlib is missing: alerts then go out as text."""
    global _warned_no_charts
    if not charts.CHARTS_AVAILABLE and not _warned_no_charts:
        logger.warning("matplotlib not installed: alerts are sent without chart images "
                       "(run .venv/bin/pip install -r requirements.txt)")
        _warned_no_charts = True
    return charts.CHARTS_AVAILABLE


def decision_for(candles: list[dict], p: dict, kind: str, cfg: AlertsConfig, bases_cfg: BaseConfig,
                 vcp_cfg: VcpConfig, live: Optional[dict] = None, force_image: bool = False,
                 size_factor: float = 1.0, size_note: str = "") -> tuple[Optional[bytes], Optional[dict]]:
    """(annotated PNG or None, decision context or None) for one alert. Never raises.
    force_image skips the chart_images / chart_kinds settings (dashboard preview)."""
    from backend.alerts.decision import decision_context, render_decision_chart
    try:
        ended = None
        if kind == "played_out":
            ended = {"date": p.get("ended_date"), "reason": p.get("ended_reason") or "",
                     "gain_pct": p.get("gain_from_pivot_pct") or 0.0, "peak_pct": p.get("peak_gain_pct") or 0.0}
        ctx = decision_context(candles, kind, symbol=p["symbol"], company=p.get("company") or "",
                               industry=p.get("industry") or "", rs=p.get("rs"), tt_passed=p.get("tt_passed") or 0,
                               tt_of=p.get("tt_of") or 8, bases_cfg=bases_cfg, vcp_cfg=vcp_cfg, alerts_cfg=cfg,
                               ended=ended, live=live, size_factor=size_factor, size_note=size_note)
        if ctx is None:
            return None, None
        png = None
        chart_kind = "intraday" if kind == "close_check" else kind      # one toggle covers both live charts
        if (force_image or (cfg.chart_images and chart_kind in cfg.chart_kinds)) and charts_available():
            png = render_decision_chart(candles, ctx)
        return png, ctx
    except Exception:
        logger.exception("Decision chart failed for %s (%s)", p.get("symbol"), kind)
        return None, None


def dispatch_scan(as_of: str, cfg: AlertsConfig, bases_cfg: BaseConfig, notifier: Notifier,
                  tokens: dict[str, int], fresh_sessions: int = 5, vcp_cfg: Optional[VcpConfig] = None) -> dict:
    """Send alerts for one as-of date. tokens: symbol -> instrument_token (for charts)."""
    vcp_cfg = vcp_cfg or VcpConfig()
    from backend.alerts.priority import MarketContext, decorate, gate
    from backend.engine import market
    sent = {"stage": 0, "near_pivot": 0, "digest": 0, "held": 0, "skipped_ready": not notifier.ready}
    if not notifier.ready:
        logger.info("Alerts not sent: Telegram not configured or alerts disabled")
        return sent
    with get_db() as conn:
        run = conn.execute("SELECT counts FROM scan_runs WHERE as_of = ? ORDER BY id DESC LIMIT 1", (as_of,)).fetchone()
        counts_all = json.loads(run["counts"]) if run else {}
        day_flags = itd.flags(conn, as_of)                  # today's 15:15 verdicts, for the cross-reference
        prev_health = market.before(conn, as_of)
    mc = MarketContext.load(cfg, as_of)
    sizing = {"size_factor": mc.size_factor, "size_note": mc.size_note}
    close_flag = {sym: next((f for f in fl if f.startswith("close_")), None) for sym, fl in day_flags.items()}
    confirmed, last_close = set(), {}
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
            send_ok, m["tier"] = gate(kind, p or {}, mc, cfg)
            if not send_ok:
                sent["held"] += 1                          # B (or muted in a correction): digest only
                continue
            cir, png, ctx = None, None, None
            if p:
                cs = candles_of(m["symbol"])
                if cs:
                    last = cs[-1]
                    rng = last["high"] - last["low"]
                    cir = (last["close"] - last["low"]) / rng if rng > 0 else None
                    png, ctx = decision_for(cs, p, kind, cfg, bases_cfg, vcp_cfg, **sizing)
            html = msg.stage_alert(kind, screen, m["text"], p or {"symbol": m["symbol"]}, cir, ctx)
            note = msg.confirm_note(close_flag.get(m["symbol"])) if kind == "breakout" else None
            if note:
                html += "\n" + note
            html = decorate(html, kind, m["tier"], mc)
            if notifier.send(f"{as_of}|{screen}|{m['symbol']}|{kind}", kind, html, m["symbol"], png):
                sent["stage"] += 1

        for sym, p in payloads.items():
            if p.get("stage") == "fresh":
                confirmed.add(sym)
            last_close.setdefault(sym, (p.get("pivot"), p.get("close")))

        if cfg.near_pivot:
            for sym, p in payloads.items():
                if p.get("stage") != "forming" or p.get("dist_to_pivot_pct") is None:
                    continue
                if p["dist_to_pivot_pct"] > cfg.near_pivot_pct * 100:
                    continue
                send_ok, t = gate("near_pivot", p, mc, cfg)
                key = f"near|{screen}|{sym}|{p['pivot']}"
                if not send_ok or notifier.already_sent(key):
                    sent["held"] += not send_ok
                    continue
                cs = candles_of(sym)
                png, ctx = decision_for(cs, p, "near_pivot", cfg, bases_cfg, vcp_cfg, **sizing) if cs else (None, None)
                if notifier.send(key, "near_pivot", decorate(msg.near_pivot_alert(screen, p, ctx), "near_pivot", t, mc),
                                 sym, png):
                    sent["near_pivot"] += 1

        if cfg.digest:
            block = msg.market_block(mc.health, prev_health) if mc.health else None
            for i, chunk in enumerate(msg.digest(screen, as_of, counts_all.get(screen, {}), moves, fresh_sessions,
                                                 market=block)):
                if notifier.send(f"{as_of}|{screen}|digest|{i}", "digest", chunk):
                    sent["digest"] += 1
    if cfg.intraday and cfg.close_check:
        for sym, flag in close_flag.items():
            if flag == "close_holding" and sym not in confirmed and sym in last_close:
                pivot, close = last_close[sym]
                if notifier.send(f"{as_of}|noconfirm|{sym}", "intraday", msg.not_confirmed_alert(sym, pivot, close), sym):
                    sent["stage"] += 1
    logger.info("Alerts for %s: %s", as_of, sent)
    return sent
