"""📖 The Week page (Portfolio › Week): one ISO week, Monday to Friday, by code from the trade tables (Real, Paper E+,
Futures, Options and the signal track's closed rows) and the equity tables. The only model text is the week's
observations (`backend/ai/week_story.py`, one `ai_notes` row of kind "week"), each tagged `ai: true`.

  week(week_iso, today)      the page's payload (GET /api/week?week=2026-W46)
  trades(frm, to)            the closed trades of the week, normalised: {book, symbol, entry, exit, net, in, out, days, why, ...}
  bounds(week_iso)           (monday, friday) ISO dates
"""

import json
from datetime import date, timedelta
from typing import Optional

from backend.database.models import get_db, now_ist
from backend.logging_config.logger import setup_logger

logger = setup_logger("engine")

BOOKS = ("real", "paper", "futures", "options", "signal")
BOOK_ICON = {"real": "💰", "paper": "📝", "futures": "📐", "options": "🧩", "signal": "⚡"}
EQUITY = (("real", "real_equity", None), ("paper", "paper_equity", "E"), ("futures", "futures_equity", "*"),
          ("options", "options_equity", None))
INDEX = "NIFTY 50"
PAPER_ACCOUNT = "E"                                    # the E+ paper account (the only swing variant)


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def parse(week_iso: str) -> tuple[int, int]:
    """'2026-W46' -> (2026, 46); ValueError on anything else."""
    s = str(week_iso).strip().upper()
    if len(s) < 7 or s[4:6] != "-W":
        raise ValueError(f"bad week {week_iso!r}: use 2026-W46")
    y, w = int(s[:4]), int(s[6:])
    date.fromisocalendar(y, w, 1)                     # raises on week 54 etc.
    return y, w


def bounds(week_iso: str) -> tuple[str, str]:
    y, w = parse(week_iso)
    return date.fromisocalendar(y, w, 1).isoformat(), date.fromisocalendar(y, w, 5).isoformat()


def days_of(week_iso: str) -> list[str]:
    y, w = parse(week_iso)
    return [date.fromisocalendar(y, w, k).isoformat() for k in range(1, 6)]


def _num(x) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


# --- the trades ---------------------------------------------------------------------------------------------------------------

def trades(frm: str, to: str) -> list[dict]:
    """Every trade closed between `frm` and `to` (inclusive) in the books, normalised (oldest first): {book, key,
    symbol, side, qty, entry, exit, net, why, in, out, days}."""
    from backend.ai import why
    out = []
    for t in why._rows("real_trades"):
        if frm <= str(t.get("out") or "") <= to:
            out.append(why._cash(t, "real"))
    for t in why._rows("paper_trades", " WHERE account = ?", (PAPER_ACCOUNT,)):
        if frm <= str(t.get("out") or "") <= to:
            out.append(why._cash(t, "paper"))
    for t in why._rows("futures_trades"):
        if frm <= str(t.get("out") or "") <= to:
            out.append({"book": "futures", "key": f"futures:{t['id']}", "symbol": t.get("symbol"), "side": t.get("side") or "long",
                        "qty": t.get("qty"), "entry": t.get("entry"), "exit": t.get("exit"), "net": t.get("net"), "why": t.get("why"),
                        "in": t.get("in"), "out": t.get("out"), "days": t.get("days")})
    with get_db() as conn:
        opts = [{**json.loads(r["payload"]), "id": r["id"], "day": r["day"]} for r in conn.execute(
            "SELECT id, day, payload FROM options_trades WHERE day >= ? AND day <= ? ORDER BY id", (frm, to))]
    for t in opts:
        out.append({"book": "options", "key": f"options:{t['id']}", "symbol": "NIFTY", "side": t.get("bias"), "qty": t.get("qty"),
                    "entry": t.get("credit"), "exit": t.get("cost"), "net": t.get("net"), "why": t.get("label") or t.get("why"),
                    "in": t["day"], "out": t["day"], "days": 0})          # the payload's in / out are clock times
    with get_db() as conn:
        flows = conn.execute("SELECT id, day, payload FROM flow_positions WHERE status = 'closed' AND day >= ? AND day <= ? ORDER BY id",
                             (frm, to)).fetchall()
    for r in flows:
        t = json.loads(r["payload"])
        out.append({"book": "signal", "key": f"signal:{r['id']}", "symbol": t.get("symbol"), "side": t.get("side"), "qty": t.get("qty"),
                    "entry": t.get("entry"), "exit": (t.get("exits") or [{}])[-1].get("price") if t.get("exits") else t.get("exit"),
                    "net": t.get("net"), "why": t.get("reason"), "in": r["day"], "out": r["day"], "days": 0})
    for t in out:
        t["net"] = round(_num(t.get("net")) or 0.0, 2)
        t["out"] = str(t.get("out") or "")[:10]
    return sorted(out, key=lambda t: (t["out"], t["book"], t["key"]))


def _won(ts: list) -> int:
    return sum(1 for t in ts if t["net"] > 0)


# --- the curve ----------------------------------------------------------------------------------------------------------------

def _series(table: str, account: Optional[str], until: str) -> dict[str, dict[str, float]]:
    """{account: {day: equity}} up to `until` (account '' for single-account tables)."""
    with get_db() as conn:
        if account is None:
            rows = conn.execute(f"SELECT '' AS account, day, equity FROM {table} WHERE day <= ? ORDER BY day", (until,)).fetchall()
        elif account == "*":
            rows = conn.execute(f"SELECT account, day, equity FROM {table} WHERE day <= ? ORDER BY day", (until,)).fetchall()
        else:
            rows = conn.execute(f"SELECT account, day, equity FROM {table} WHERE account = ? AND day <= ? ORDER BY day", (account, until)).fetchall()
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out.setdefault(r["account"], {})[r["day"]] = float(r["equity"])
    return out


def _at(series: dict[str, float], day: str) -> Optional[float]:
    """The equity on `day`, else the last one before it (carried), else None."""
    best = None
    for d in sorted(series):
        if d > day:
            break
        best = series[d]
    return best


def _index_closes(frm: str, to: str) -> tuple[Optional[float], dict[str, float]]:
    """(NIFTY 50's close before `frm`, {day: close} from `frm` to `to`) from candles_daily; without stored NIFTY 50
    candles, the NIFTY close the books' equity rows keep (their `nifty` column, written by the same evening job)."""
    with get_db() as conn:
        inst = conn.execute("SELECT instrument_token FROM instruments WHERE tradingsymbol = ? ORDER BY segment = 'INDICES' DESC LIMIT 1",
                            (INDEX,)).fetchone()
        if inst:
            tok = inst[0]
            base = conn.execute("SELECT close FROM candles_daily WHERE instrument_token = ? AND date < ? ORDER BY date DESC LIMIT 1", (tok, frm)).fetchone()
            rows = conn.execute("SELECT date, close FROM candles_daily WHERE instrument_token = ? AND date >= ? AND date <= ? ORDER BY date",
                                (tok, frm, to)).fetchall()
            if rows:
                return (float(base[0]) if base else None), {r["date"][:10]: float(r["close"]) for r in rows}
        for _, table, _a in EQUITY:
            rows = conn.execute(f"SELECT day, MAX(nifty) AS nifty FROM {table} WHERE nifty IS NOT NULL AND day >= ? AND day <= ? GROUP BY day ORDER BY day",
                                (frm, to)).fetchall()
            if rows:
                base = conn.execute(f"SELECT nifty FROM {table} WHERE nifty IS NOT NULL AND day < ? ORDER BY day DESC LIMIT 1", (frm,)).fetchone()
                return (float(base[0]) if base else None), {r["day"]: float(r["nifty"]) for r in rows}
    return None, {}


def curve(frm: str, to: str, today: str) -> tuple[list[dict], dict]:
    """([{t, all_books, nifty}], {nifty_pct by day}): the books' equity summed and normalised to the week's start (the
    last stored equity before Monday, else the book's first value that week) in %, NIFTY 50's close in % of its close
    before Monday. One point per day Mon–Fri up to today that has a value."""
    days = [d for d in (date.fromisoformat(frm) + timedelta(days=k) for k in range(5)) if d.isoformat() <= min(to, today)]
    accts = []
    for _, table, acct in EQUITY:
        try:
            for s in _series(table, acct, to).values():
                accts.append(s)
        except Exception:
            logger.exception("week: the %s equity failed", table)
    day_before = (date.fromisoformat(frm) - timedelta(days=1)).isoformat()
    base_n, idx = _index_closes(frm, to)
    pts, prev_close, nifty_day = [], base_n, {}
    for d in days:
        iso = d.isoformat()
        vals = [(s, _at(s, iso)) for s in accts]
        live = [(s, v) for s, v in vals if v is not None and any(k <= iso for k in s)]
        base = 0.0
        now_sum = 0.0
        for s, v in live:
            b = _at(s, day_before)
            if b is None:
                b = s[min(k for k in s if k >= frm)] if any(k >= frm for k in s) else v
            base += b
            now_sum += v
        all_books = round((now_sum / base - 1) * 100, 2) if live and base else None
        c = idx.get(iso)
        nifty = round((c / base_n - 1) * 100, 2) if c and base_n else None
        if c and prev_close:
            nifty_day[iso] = round((c / prev_close - 1) * 100, 2)
        if c:
            prev_close = c
        if all_books is not None or nifty is not None:
            pts.append({"t": iso, "all_books": all_books, "nifty": nifty})
    return pts, nifty_day


# --- the rest of the page ---------------------------------------------------------------------------------------------------------

def _score_at(symbol: Optional[str], day: Optional[str]) -> Optional[int]:
    if not symbol or not day:
        return None
    try:
        from backend.ai import view
        v = view.score_for(symbol, str(day)[:10] + "~")       # the latest view on or before the entry day
        return v["score"] if v else None
    except Exception:
        return None


def _pick(t: Optional[dict]) -> Optional[dict]:
    if not t:
        return None
    return {"symbol": t["symbol"], "book": t["book"], "net": t["net"], "score": _score_at(t["symbol"], t.get("in")),
            "ended": t["out"], "why": t.get("why")}


def _suggestions(frm: str, to: str) -> dict:
    with get_db() as conn:
        rows = conn.execute("SELECT status, outcome FROM ai_suggestions WHERE day >= ? AND day <= ?", (frm, to)).fetchall()
    helped = 0
    for r in rows:
        try:
            o = json.loads(r["outcome"]) if r["outcome"] else None
        except ValueError:
            o = None
        if r["status"] == "accepted" and o and o.get("helped"):
            helped += 1
    return {"made": len(rows), "accepted": sum(1 for r in rows if r["status"] == "accepted"), "helped": helped}


def _ai_view(week_iso: str) -> dict:
    try:
        from backend.ai import view
        w = next((x for x in view.by_week(view.rows(" WHERE status = 'ok'")) if x["week"] == week_iso), None)
    except Exception:
        logger.exception("week: the AI view record failed")
        w = None
    return {"buy_hit": (w or {}).get("buy_hit20"), "baseline": (w or {}).get("baseline20"), "votes": (w or {}).get("votes", 0)}


def _backtest_hint(frm: str, to: str) -> Optional[dict]:
    """The suggest `backtest` kind's prefill when one was born that week."""
    with get_db() as conn:
        r = conn.execute("SELECT id, title, action FROM ai_suggestions WHERE kind = 'backtest' AND day >= ? AND day <= ? ORDER BY id DESC LIMIT 1",
                         (frm, to)).fetchone()
    if not r:
        return None
    a = json.loads(r["action"]) if r["action"] else {}
    return {"id": r["id"], "title": r["title"], "route": a.get("route"), "prefill": a.get("prefill") or {}, "label": a.get("label")}


def _signed_pts(x: float) -> str:
    return f"{abs(x):.1f} pts"


def headline(net: float, n: int, won: int, vs: Optional[float]) -> str:
    """'+₹6,900 · 9 trades · 5 won · beat NIFTY by 1.2 pts' (code)."""
    from backend.alerts.messages import inr
    bits = [inr(net, sign=True) if n else "no closed trades", f"{n} trade{'s' if n != 1 else ''}", f"{won} won"] if n else ["no closed trades"]
    if vs is not None:
        bits.append(f"beat NIFTY by {_signed_pts(vs)}" if vs > 0 else f"trailed NIFTY by {_signed_pts(vs)}" if vs < 0 else "level with NIFTY")
    return " · ".join(bits)


def week(week_iso: Optional[str] = None, today: Optional[str] = None) -> dict:
    """GET /api/week: {week, from, to, headline, totals {net, trades, won, vs_nifty_pts}, by_day [{day, net, trades, won,
    nifty_pct}], curve [{t, all_books, nifty}], by_book [{book, icon, net, trades, won}], best, worst, suggestions {made,
    accepted, helped}, ai_view {buy_hit, baseline}, backtest_hint, observations [{text, ai, checked, day}], prev_week,
    next_week, story {at, model, rows_checked, running}, trades}. Week default: the current ISO week."""
    today = today or now_ist()[:10]
    week_iso = week_iso or iso_week(date.fromisoformat(today))
    y, w = parse(week_iso)
    week_iso = f"{y}-W{w:02d}"
    frm, to = bounds(week_iso)
    ts = trades(frm, to)
    pts, nifty_day = curve(frm, to, today)
    net = round(sum(t["net"] for t in ts), 2)
    last = pts[-1] if pts else {}
    vs = round(last["all_books"] - last["nifty"], 1) if last.get("all_books") is not None and last.get("nifty") is not None else None
    by_day = []
    for d in days_of(week_iso):
        g = [t for t in ts if t["out"] == d]
        by_day.append({"day": d, "net": round(sum(t["net"] for t in g), 2), "trades": len(g), "won": _won(g), "nifty_pct": nifty_day.get(d)})
    by_book = []
    for b in BOOKS:
        g = [t for t in ts if t["book"] == b]
        if g:
            by_book.append({"book": b, "icon": BOOK_ICON[b], "net": round(sum(t["net"] for t in g), 2), "trades": len(g), "won": _won(g)})
    stocks = [t for t in ts if t["book"] != "options"] or ts
    best = max(stocks, key=lambda t: t["net"], default=None)
    worst = min(stocks, key=lambda t: t["net"], default=None)
    from backend.ai import week_story
    story = week_story.latest(week_iso)
    mon = date.fromisocalendar(y, w, 1)
    prev_w = iso_week(mon - timedelta(days=7))
    nxt = mon + timedelta(days=7)
    next_w = iso_week(nxt) if nxt.isoformat() <= today else None
    return {"week": week_iso, "from": frm, "to": to, "label": f"Week {w}", "headline": headline(net, len(ts), _won(ts), vs),
            "totals": {"net": net, "trades": len(ts), "won": _won(ts), "vs_nifty_pts": vs},
            "by_day": by_day, "curve": pts, "by_book": by_book,
            "best": _pick(best), "worst": _pick(worst if worst is not best else None),
            "suggestions": _suggestions(frm, to), "ai_view": _ai_view(week_iso), "backtest_hint": _backtest_hint(frm, to),
            "observations": (story or {}).get("observations") or [], "prev_week": prev_w, "next_week": next_w,
            "story": {"at": (story or {}).get("at"), "at_label": (story or {}).get("at_label"), "model": (story or {}).get("model"),
                      "rows_checked": (story or {}).get("rows_checked"), "running": week_story.running(week_iso)},
            "trades": [{k: t.get(k) for k in ("book", "symbol", "entry", "exit", "net", "in", "out", "days", "why")} for t in ts]}
