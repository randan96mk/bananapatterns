"""Sample alerts for testing the Telegram setup at any time (weekends, holidays, no Kite login).

Each sample is built exactly like the real alert of that kind, using a stock from
the latest scan when one fits (so the text and chart look real), else a built-in
example. Every message starts with "🧪 TEST —". Samples bypass the send-once
rule and use their own log keys (kind "test"), so they never block a real alert.
"""

import json
import math
import uuid
from typing import Optional

from backend.alerts import messages as msg
from backend.alerts.dispatch import decision_for
from backend.alerts.telegram import TelegramClient, TelegramError
from backend.config import AppConfig
from backend.database.models import get_candles, get_db, now_ist

SAMPLE_KINDS = ["breakout", "near_pivot", "forming", "below_pivot", "played_out", "intraday", "failed",
                "close_check", "digest", "system"]
PREFIX = "🧪 <b>TEST</b> — "


def _example_candles() -> list[dict]:
    """Deterministic example: advance, a base with shrinking pullbacks, breakout on the last bar."""
    from datetime import date, timedelta
    closes = [80 + 20 * i / 60 for i in range(60)]                              # 80 -> 100
    for depth, n in ((0.10, 16), (0.06, 12), (0.03, 8)):                        # pullbacks 10% / 6% / 3%
        closes += [100 * (1 - depth * math.sin(math.pi * k / n)) for k in range(1, n + 1)]
    closes.append(103.0)                                                       # breakout close
    out, d, prev = [], date(2026, 4, 1), closes[0]
    for i, c in enumerate(closes):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        vol = 3_000_000 if i == len(closes) - 1 else int(1_000_000 * (1.2 - 0.5 * min(i, 120) / 120))
        out.append({"date": d.isoformat(), "open": prev, "high": max(prev, c) * 1.004, "low": min(prev, c) * 0.996,
                    "close": c, "volume": vol})
        prev = c
        d += timedelta(days=1)
    return out


EXAMPLE = {"symbol": "EXAMPLE", "company": "Example Industries Ltd.", "industry": "Capital Goods", "rs": 91,
           "tt_passed": 8, "tt_of": 8, "base_label": "B2 · 7w", "pivot": 100.0, "close": 103.0, "change_pct": 3.4,
           "gain_from_pivot_pct": 3.0, "volume_ratio": 2.6, "dist_to_pivot_pct": 1.8, "ended_reason":
           "closed below its 50-day line", "peak_gain_pct": 14.2, "breakout_date": None,
           "vcp": {"stop": 96.5, "contractions": [{"depth_pct": 10.0}, {"depth_pct": 6.0}, {"depth_pct": 3.0}]}}


def _latest_rows() -> tuple[Optional[str], list[dict]]:
    with get_db() as conn:
        as_of = conn.execute("SELECT MAX(as_of) FROM scan_runs").fetchone()[0]
        if not as_of:
            return None, []
        rows = conn.execute("SELECT screen, symbol, stage, payload FROM scan_results WHERE as_of = ?",
                            (as_of,)).fetchall()
    return as_of, [dict(json.loads(r["payload"]), screen=r["screen"], stage=r["stage"], symbol=r["symbol"])
                   for r in rows]


def _pick(rows: list[dict], stages: tuple, key=None) -> Optional[dict]:
    """Prefer the VCP screen, then Bases; within a screen sort by `key`."""
    for screen in ("vcp", "bases"):
        cands = [r for r in rows if r["screen"] == screen and r["stage"] in stages]
        if cands:
            return sorted(cands, key=key)[0] if key else cands[0]
    return None


def build_samples(kind: str, config: AppConfig, tokens: dict[str, int]) -> list[tuple[str, str, Optional[bytes]]]:
    """[(kind, html, png or None)] for one kind or "all", with the same decision chart and lines as real alerts."""
    kinds = SAMPLE_KINDS if kind == "all" else [kind]
    as_of, rows = _latest_rows()
    out = []

    def candles_for(p, k):
        tok = tokens.get(p["symbol"])
        cs = None
        if tok is not None:
            with get_db() as conn:
                cs = get_candles(conn, tok)
        if not cs:
            cs = _example_candles()
            if k not in ("breakout", "below_pivot", "played_out"):  # intraday kinds: the bars before today
                cs = cs[:-1]                                # example forming: before the breakout bar
        return cs

    def decision(p, k, live=None):
        cs = candles_for(p, k)
        return decision_for(cs, p, k, config.alerts, config.bases, config.vcp, live=live)

    for k in kinds:
        if k == "breakout":
            p = _pick(rows, ("fresh", "fresh_low_volume"), key=lambda r: r.get("sessions_since_breakout") or 0) \
                or EXAMPLE
            text = (f"{p['symbol']} broke out of {p['base_label']}: closed {msg.rs(p['close'])}, "
                    f"{p.get('gain_from_pivot_pct') or 0:+.1f}% over the {msg.rs(p['pivot'])} pivot on "
                    f"{p.get('volume_ratio') or 0:.1f}x volume.")
            png, ctx = decision(p, k)
            out.append((k, PREFIX + msg.stage_alert(k, p.get("screen", "vcp"), text, p, 0.8, ctx), png))
        elif k == "near_pivot":
            p = _pick(rows, ("forming",), key=lambda r: r.get("dist_to_pivot_pct") or 99) or EXAMPLE
            png, ctx = decision(p, k)
            out.append((k, PREFIX + msg.near_pivot_alert(p.get("screen", "vcp"), p, ctx), png))
        elif k == "forming":
            p = _pick(rows, ("forming",), key=lambda r: -(r.get("score") or r.get("rs") or 0)) or EXAMPLE
            text = (f"{p['symbol']} is newly forming {p['base_label']}: {p.get('dist_to_pivot_pct') or 0:.1f}% under "
                    f"the {msg.rs(p['pivot'])} pivot.")
            png, ctx = decision(p, k)
            out.append((k, PREFIX + msg.stage_alert(k, p.get("screen", "vcp"), text, p, ctx=ctx), png))
        elif k == "below_pivot":
            p = _pick(rows, ("below_pivot", "climbing", "fresh")) or EXAMPLE
            text = f"{p['symbol']} closed {msg.rs(p['close'])}, back below the {msg.rs(p['pivot'])} pivot it broke out of."
            png, ctx = decision(p, k)
            out.append((k, PREFIX + msg.stage_alert(k, p.get("screen", "vcp"), text, p, ctx=ctx), png))
        elif k == "played_out":
            p = _pick(rows, ("played_out",)) or EXAMPLE
            text = (f"{p['symbol']} played out. Closed {msg.rs(p['close'])}, {p.get('ended_reason') or 'run ended'}, "
                    f"{p.get('gain_from_pivot_pct') or 0:+.1f}% from the {msg.rs(p['pivot'])} pivot "
                    f"(peak {p.get('peak_gain_pct') or 0:+.1f}%).")
            png, ctx = decision(p, k)
            out.append((k, PREFIX + msg.stage_alert(k, p.get("screen", "vcp"), text, p, ctx=ctx), png))
        elif k == "intraday":
            p = _pick(rows, ("forming",), key=lambda r: r.get("dist_to_pivot_pct") or 99) or EXAMPLE
            cs = candles_for(p, k)
            ltp = round(p["pivot"] * 1.006, 2)
            prev = cs[-1]["close"]
            live = {"ltp": ltp, "open": prev, "high": ltp * 1.003, "low": min(prev, ltp) * 0.995, "volume": 1.1e6}
            png, ctx = decision_for(cs, p, k, config.alerts, config.bases, config.vcp, live=live)
            out.append((k, PREFIX + msg.intraday_alert(p, ltp, 1.1e6, 2.4, 1.0e6, ctx, volume="strong",
                                                       zone_top=p["pivot"] * (1 + config.alerts.buy_zone_pct)), png))
        elif k == "failed":
            p = _pick(rows, ("forming",), key=lambda r: r.get("dist_to_pivot_pct") or 99) or EXAMPLE
            out.append((k, PREFIX + msg.failed_alert(p, round(p["pivot"] * 0.985, 2), round(p["pivot"] * 1.012, 2),
                                                     (p.get("vcp") or {}).get("stop")), None))
        elif k == "close_check":
            p = _pick(rows, ("forming",), key=lambda r: r.get("dist_to_pivot_pct") or 99) or EXAMPLE
            cs = candles_for(p, k)
            ltp = round(p["pivot"] * 1.018, 2)
            prev = cs[-1]["close"]
            low = min(prev, p["pivot"]) * 0.995
            live = {"ltp": ltp, "open": prev, "high": ltp * 1.004, "low": low, "volume": 1.6e6,
                    "pace": 1.8, "range": (ltp - low) / (ltp * 1.004 - low), "verdict": "holding"}
            png, ctx = decision_for(cs, p, k, config.alerts, config.bases, config.vcp, live=live)
            out.append((k, PREFIX + msg.close_alert(p, "holding", ltp, 1.8, live["range"],
                                                    p["pivot"] * (1 + config.alerts.buy_zone_pct), ctx), png))
        elif k == "digest":
            screen = "vcp" if any(r["screen"] == "vcp" for r in rows) else "bases"
            with get_db() as conn:
                run = conn.execute("SELECT counts FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
                moves = [dict(r) for r in conn.execute(
                    "SELECT kind, text, change_pct, close FROM scan_changes WHERE as_of = ? AND screen = ? LIMIT 8",
                    (as_of, screen))] if as_of else []
            counts = json.loads(run["counts"]).get(screen, {}) if run else {"forming": 3, "fresh": 1}
            if not moves:
                moves = [{"kind": "breakout", "text": "EXAMPLE broke out of B2 · 7w: closed ₹103.00, +3.0% over the "
                          "₹100.00 pivot on 2.6x volume.", "close": 103.0, "change_pct": 3.4}]
            chunk = msg.digest(screen, as_of or "sample", counts, moves)[0]
            out.append((k, PREFIX + chunk, None))
        elif k == "system":
            out.append((k, PREFIX + msg.system_alert("Kite login needed for today's 18:30 evening scan "
                                                     "(tokens expire 06:00 IST). Open the dashboard and log in."), None))
        else:
            raise ValueError(f"Unknown sample kind {k!r}; use one of {SAMPLE_KINDS} or 'all'")
    return out


def send_samples(kind: str, config: AppConfig, tokens: dict[str, int], chat_id: str,
                 client: Optional[TelegramClient] = None) -> list[dict]:
    """Send the samples and log each one (kind 'test'). Returns [{kind, ok, error}]."""
    client = client or TelegramClient()
    results = []
    for k, html, png in build_samples(kind, config, tokens):
        ok, err = True, None
        try:
            if png:
                client.send_photo(chat_id, png, html)
            else:
                client.send_message(chat_id, html)
        except TelegramError as exc:
            ok, err = False, str(exc)
        stamp = now_ist()
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO sent_alerts (key, sent_at, kind, symbol, text, ok, error) "
                         "VALUES (?,?,?,?,?,?,?)", (f"test|{uuid.uuid4().hex}", stamp, "test", k, html, int(ok), err))
        results.append({"kind": k, "ok": ok, "error": err, "chart": png is not None})
        if not ok and "Unauthorized" in (err or ""):
            break                                        # a bad token fails every message: stop early
    return results
