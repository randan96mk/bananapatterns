"""The News tab: exchange news on the stocks that matter to you, in one list.

Scope ("board stocks"): every stock in the latest scan on any screen (VCP, Cup & handle, Bases) at any stage,
plus the stocks you hold (position guard) or watch. Built from what is already stored, no extra NSE calls:
  results   📊 results filed                       filings
  deals     📦 order wins, 🤝 deals                 filings
  red       🚩 promoter selling, legal action, auditor exit, rating downgrade      holders.signals
  funds     🏦 funds buying / selling (bulk / block deals, stake changes)          holders.signals
  other     🏷️ ratings, 👔 management changes, ❓ exchange queries                filings
Plus the results calendar: who reports within results_warn_sessions.
"""

from datetime import date, timedelta
from typing import Optional

from backend.database.models import get_db, now_ist
from backend.ingest import events
from backend.ingest import filings as fl
from backend.ingest import holders

SCREEN_NAMES = {"vcp": "VCP", "cup": "C&H", "bases": "Base"}
STAGE_NAMES = {"forming": "forming", "fresh": "fresh breakout", "fresh_low_volume": "low-vol breakout",
               "climbing": "climbing", "played_out": "played out", "below_pivot": "below pivot"}
SCREEN_ORDER = {"vcp": 0, "cup": 1, "bases": 2}
GROUP = {"results": "results", "order": "deals", "deal": "deals", "rating": "other", "management": "other",
         "legal": "other", "exchange": "other"}
ICONS = {"results": "📊", "order": "📦", "deal": "🤝", "rating": "🏷️", "management": "👔", "legal": "⚖️",
         "exchange": "❓"}


def board_scope() -> dict[str, str]:
    """{symbol: why}: held / watched first, then the most specific screen and stage it is on."""
    from backend.alerts import guard
    scope: dict[str, str] = {p["symbol"]: "held" for p in guard.positions()}
    with get_db() as conn:
        for r in conn.execute("SELECT symbol FROM watchlist"):
            scope.setdefault(r["symbol"], "watching")
        rows = conn.execute("SELECT symbol, screen, stage FROM scan_results WHERE as_of = "
                            "(SELECT MAX(as_of) FROM scan_runs)").fetchall()
    for r in sorted(rows, key=lambda r: SCREEN_ORDER.get(r["screen"], 3)):
        scope.setdefault(r["symbol"], f"{SCREEN_NAMES.get(r['screen'], r['screen'])} "
                                      f"{STAGE_NAMES.get(r['stage'], r['stage'])}")
    return scope


def _why_rank(why: str) -> int:
    """held, watched, then live setups (forming / breakouts / climbing), then the rest."""
    if why in ("held", "watching"):
        return 0 if why == "held" else 1
    return 2 if any(s in why for s in ("forming", "breakout", "climbing")) else 3


def board_news(cfg, days: Optional[int] = None, today: Optional[str] = None) -> dict:
    """{items: [...] newest first, results: [...] nearest first, counts: {group: n}, scope: n}."""
    today = today or now_ist()[:10]
    days = days or cfg.news_days
    since = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    scope = board_scope()
    syms = list(scope)
    items: list[dict] = []
    if syms and cfg.filings:
        qs = ",".join("?" * len(syms))
        with get_db() as conn:
            rows = [dict(r) for r in conn.execute(
                f"SELECT seq_id, symbol, dt, kind, category, text, url FROM filings WHERE symbol IN ({qs}) "
                f"AND dt >= ? AND kind IN ({','.join('?' * len(fl.IMPORTANT))})", (*syms, since, *fl.IMPORTANT))]
        for f in rows:
            if cfg.red_flags and holders.filing_red_label(f):
                continue                                   # listed once, as a 🚩 red flag below
            items.append({"group": GROUP[f["kind"]], "icon": ICONS[f["kind"]], "symbol": f["symbol"],
                          "why": scope[f["symbol"]], "label": fl.LABELS[f["kind"]], "text": f["text"] or f["category"],
                          "dt": f["dt"], "url": f["url"]})
    for sym, s in holders.signals(syms, cfg, today).items():
        for key, group, icon in (("red", "red", "🚩"), ("buy", "funds", "🏦"), ("sell", "funds", "🏦")):
            for i in s[key]:
                if i["date"] >= since:
                    items.append({"group": group, "icon": icon, "symbol": sym, "why": scope[sym],
                                  "label": i["label"], "text": i["text"], "dt": i["date"], "url": i.get("url", ""),
                                  "side": key})
    items.sort(key=lambda i: i["dt"], reverse=True)
    seen, unique = set(), []
    for i in items:                                        # results + their press release: one line a day
        k = (i["symbol"], i["group"], i["label"], i["dt"][:10])
        if k not in seen:
            seen.add(k)
            unique.append(i)
    items = unique
    results = []
    if syms and cfg.results_calendar:
        for sym, r in events.upcoming_results(today, syms).items():
            if r["sessions"] <= cfg.results_warn_sessions:
                results.append(dict(r, symbol=sym, why=scope[sym], label=events.results_label(r)))
        results.sort(key=lambda r: (r["sessions"], _why_rank(r["why"]), r["symbol"]))
    counts: dict[str, int] = {}
    for i in items:
        counts[i["group"]] = counts.get(i["group"], 0) + 1
    return {"as_of": today, "days": days, "scope": len(scope), "items": items, "results": results, "counts": counts}
