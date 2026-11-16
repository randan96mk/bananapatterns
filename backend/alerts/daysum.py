"""🌙 The day summary's facts: one dict per book that had anything today, for `messages.day_summary` (sent once at
alerts.day_summary_time by the scheduler's `run_day_summary`). Read-only: every part reads the stored state and
tables, never Kite, and a part that fails is left out (the others still go).

  real_part(config, day)      💰 value (live where the stop watch saw it), today's P&L, stops hit, failed orders,
                                 tomorrow's BUYs / EXITs once the evening has planned them
  paper_parts(config, day)    📝 per account: value, today's P&L once marked (the evening job), stops hit
  futures_part(config, day)   📐 the long side's primary account: value, today's MTM, open lots, the roll day
  options_part(config, day)   🧩 the options_days row of `day` (written at 3:35 PM): equity, P&L, trades, result
  flow_part(config, day)      ⚡ the signal track / paper intraday: trades, won, net on the capital (only with trades)
  ai_part(config, day)        🤖 tonight's AI work, or what last night's run made (notes, suggestions, ratings)
  parts(config, day)          all of the above, keyed by book, books with nothing left out
"""

import json
from typing import Optional

from backend.config import AppConfig
from backend.database.models import get_db
from backend.logging_config.logger import setup_logger

logger = setup_logger("alerts")


def _prev_equity(table: str, day: str, account: Optional[str] = None) -> Optional[float]:
    """The last stored equity before `day` (the previous session's close), None when there is none."""
    where, args = ("account = ? AND ", (account, day)) if account else ("", (day,))
    with get_db() as conn:
        r = conn.execute(f"SELECT equity FROM {table} WHERE {where}day < ? ORDER BY day DESC LIMIT 1", args).fetchone()
    return r[0] if r else None


def _added_on(deposits: list, day: str) -> float:
    """Money added (or withdrawn) on `day`: not a profit, so out of the day's P&L."""
    return sum(x["amount"] for x in deposits or [] if str(x.get("at", ""))[:10] == day)


def real_part(config: AppConfig, day: str) -> Optional[dict]:
    """💰 {value, today, stops, failed, tomorrow}; None before real trading started."""
    from backend.engine import real
    st = real._state()
    if not st:
        return None
    value = st["cash"] + sum(x["qty"] * (x.get("live") or x["last"]) for x in st["positions"].values())
    prev = _prev_equity("real_equity", day)
    base = (prev if prev is not None else st["capital"] - sum(x["amount"] for x in st.get("deposits", [])))
    with get_db() as conn:
        stops = [r[0] for r in conn.execute("SELECT symbol FROM real_orders WHERE day = ? AND status = 'sent' AND reason LIKE 'stop%' "
                                            "ORDER BY id", (day,))]
        failed = conn.execute("SELECT COUNT(*) FROM real_orders WHERE day = ? AND status = 'failed'", (day,)).fetchone()[0]
    tomorrow = None
    if st.get("as_of") == day:                            # the evening has planned the next open's orders
        orders = st.get("orders") or []
        tomorrow = {"buys": sum(1 for o in orders if o.get("action") == "BUY"), "exits": sum(1 for o in orders if o.get("action") == "EXIT")}
    return {"value": round(value, 2), "today": round(value - base - _added_on(st.get("deposits"), day), 2), "stops": stops,
            "failed": failed, "tomorrow": tomorrow}


def paper_parts(config: AppConfig, day: str) -> list[dict]:
    """📝 One {name, value, today, stops, marks_at} per paper account; `today` is None until the evening job marks it."""
    from backend.engine import paper
    out = []
    for key, st in sorted(paper._states().items()):
        value = paper.account(key, st)["value"]
        today = None
        if st.get("as_of") == day:
            prev = _prev_equity("paper_equity", day, key)
            if prev is not None:
                today = round(value - prev - _added_on(st.get("deposits"), day), 2)
        with get_db() as conn:
            closed = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM paper_trades WHERE account = ?", (key,))]
        stops = sum(1 for t in closed if t.get("out") == day and str(t.get("why") or "").lower().startswith("stop"))
        out.append({"name": str(st.get("name") or key).split(" · ")[0], "value": value, "today": today, "stops": stops,
                    "marks_at": config.alerts.job_time})
    return out


def futures_part(config: AppConfig, day: str) -> Optional[dict]:
    """📐 {tag, value, today, lots, roll} of the long side's primary account; None without a futures book."""
    from backend.futures import book
    st = book._state()
    if not st:
        return None
    pk = book.primary(config, "long")
    a = st["accounts"][pk]
    value = book._value(a)
    roll = st.get("roll_day")
    return {"tag": book.mode_of(pk), "value": round(value, 2), "today": round(value - (a.get("day_start") or st["capital"]), 2),
            "lots": sum(int(p.get("lots") or 0) for p in a["positions"].values()), "roll": roll if roll and roll >= day else None}


def options_part(config: AppConfig, day: str) -> Optional[dict]:
    """🧩 {tag, value, today, trades, result} from the options_days row of `day`; None without one."""
    from backend.options import book as obook
    with get_db() as conn:
        r = conn.execute("SELECT payload FROM options_days WHERE day = ?", (day,)).fetchone()
    if not r:
        return None
    row = json.loads(r[0])
    n = row.get("trades") or 0
    return {"tag": obook.TAG, "value": row.get("equity"), "today": row.get("pnl") or 0.0, "trades": n,
            "result": row.get("result") if n else row.get("why")}


def flow_part(config: AppConfig, day: str) -> Optional[dict]:
    """⚡ {kind, trades, won, net, capital} of the day's intraday flow; None when it is off or made no trade."""
    if config.flow.mode == "off":
        return None
    from backend.intraday import flow
    st = flow._stats(day)
    if not st.get("trades"):
        return None
    return {"kind": "signal" if config.flow.mode == "signals" else "paper", "trades": st["trades"], "won": st.get("won", 0),
            "net": st.get("realised", 0.0), "capital": st.get("capital")}


def ai_part(config: AppConfig, day: str) -> Optional[dict]:
    """🤖 {ran, notes, suggestions, rated} when the night's AI work for `day` has made anything, else {ran: False,
    after, parts}: what runs tonight after the evening job. None while the AI is off."""
    if not config.ai.enabled:
        return None
    with get_db() as conn:
        n_notes = conn.execute("SELECT COUNT(*) FROM ai_notes WHERE day = ? AND kind IN ('filing', 'deal')", (day,)).fetchone()[0]
        n_sugg = conn.execute("SELECT COUNT(*) FROM ai_suggestions WHERE day = ?", (day,)).fetchone()[0]
        n_rated = conn.execute("SELECT COUNT(*) FROM ai_views WHERE day = ? AND status = 'ok'", (day,)).fetchone()[0]
    if n_notes or n_sugg or n_rated:
        return {"ran": True, "notes": n_notes, "suggestions": n_sugg, "rated": n_rated}
    parts = ["notes"] + (["why"] if config.ai.why else []) + ["suggestions"] + (["ratings"] if config.ai.view_enabled else [])
    return {"ran": False, "after": config.alerts.job_time, "parts": parts}


PARTS = (("real", real_part), ("paper", paper_parts), ("futures", futures_part), ("options", options_part), ("flow", flow_part),
         ("ai", ai_part))


def parts(config: AppConfig, day: str) -> dict:
    """{book: part} for every book with something to say on `day` (a part that fails is logged and left out)."""
    out = {}
    for key, fn in PARTS:
        try:
            p = fn(config, day)
        except Exception:
            logger.exception("Day summary: the %s part failed", key)
            continue
        if p:
            out[key] = p
    return out
