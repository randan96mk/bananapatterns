"""🧩 Report as if · exit rules: every closed spread in `options_trades` replayed on its day's 5-minute chain snapshots
under other exit rules, so a change to the stops can be SEEN before it is switched on.

The replay starts from the trade's real entry (`sold.entry`, `hedge.entry`, `credit`, `qty`, `opened_at`) and steps
through the `options_chain_snap` rows of that day (kinds live / bias / close, the trade's expiry, ts order) for the two
legs: mid = (bid + ask) / 2 when both are there, else the last price. On each snapshot before the time exit the rule
is applied as the book does it: breakeven armed after `be_after_pct` decay, the premium stop (`stop_pct`, or
`stop_pct_expiry` on expiry day), the breakeven stop, the target. The first snapshot at or after the time exit closes
it (15:10 live, else the 15:30 close snapshot, else the last one seen).

VWAP exits: the snapshots carry no VWAP path, so a VWAP exit is never invented. When the real trade ended on a VWAP stop
and the rule has VWAP on, the replay exits at that real time at the real fills (the real decision); with VWAP off it
ignores that exit and keeps stepping. After a real price stop no VWAP is known any more, so the replay goes on without it.
Exit fills as the book: buy the sold leg back at mid + 1, sell the hedge at mid - 1; charges via the book's `charges()`.
Paper only; nothing here trades."""

import json
from datetime import date, datetime, timedelta
from typing import Optional

from backend.config import IST, AppConfig
from backend.database.models import get_db
from backend.options import bias as bs
from backend.options.book import charges
from backend.timefmt import ampm

KINDS = ("live", "bias", "close")
THIN_GAP_MIN = 30                       # a gap over this many minutes between two snapshots used = "thin" coverage
WHY = {"premium stop": "premium", "breakeven stop": "breakeven", "target": "target", "vwap stop": "vwap", "time exit": "time"}
NOTE = "replayed from the 5-minute chain snapshots · VWAP exits kept at their real time, never invented · paper only"


def times_x(stop_pct: float) -> str:
    """'2×' from stop_pct 100, '1.5×' from 50."""
    return f"{1 + float(stop_pct) / 100:g}×"


def rules(config: AppConfig) -> list[dict]:
    """The rules compared: today's saved settings first, then the fixed alternatives."""
    c = config.options
    asis = {"key": "asis", "label": f"as is · {times_x(c.stop_pct)}{' + VWAP' if c.vwap_exit else ' only'}", "stop_pct": float(c.stop_pct),
            "stop_pct_expiry": float(c.stop_pct_expiry), "vwap": bool(c.vwap_exit), "actual": True}
    return [asis,
            {"key": "x2", "label": "2× only", "stop_pct": 100.0, "stop_pct_expiry": 70.0, "vwap": False},
            {"key": "x2_vwap", "label": "2× + VWAP", "stop_pct": 100.0, "stop_pct_expiry": 70.0, "vwap": True},
            {"key": "x15", "label": "1.5× only", "stop_pct": 50.0, "stop_pct_expiry": 35.0, "vwap": False}]


def _mid(r: dict) -> Optional[float]:
    if r.get("bid") and r.get("ask"):
        return (r["bid"] + r["ask"]) / 2
    return r.get("ltp")


def _dt(ts: str) -> datetime:
    t = datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=IST)


def _opened(t: dict) -> Optional[datetime]:
    if t.get("opened_at"):
        return _dt(t["opened_at"])
    if t.get("in"):                                             # an older row: the day + its 12-hour entry time
        hh, rest = t["in"].split(":")
        m, ap = rest.split(" ")
        h = int(hh) % 12 + (12 if ap == "PM" else 0)
        return datetime.combine(date.fromisoformat(t["day"]), datetime.min.time(), tzinfo=IST).replace(hour=h, minute=int(m))
    return None


def _closed(t: dict) -> Optional[datetime]:
    if t.get("closed_at"):
        return _dt(t["closed_at"])
    if t.get("out"):
        return _opened({"day": t["day"], "in": t["out"]})
    return None


def path(t: dict) -> list[dict]:
    """The legs' mids on the trade's day after its entry: [{ts, at (datetime), sold, hedge}] in ts order (a snapshot
    without both legs is left out)."""
    s, h = t["sold"], t["hedge"]
    with get_db() as conn:
        rows = conn.execute(f"SELECT ts, strike, type, ltp, bid, ask FROM options_chain_snap WHERE day = ? AND expiry = ? AND kind IN ({','.join('?' * len(KINDS))}) "
                            "AND type = ? AND strike IN (?, ?) ORDER BY ts, id", (t["day"], t["expiry"], *KINDS, s["type"], s["strike"], h["strike"])).fetchall()
    by: dict[str, dict] = {}
    for r in rows:
        r = dict(r)
        k = by.setdefault(r["ts"], {})
        leg = "sold" if r["strike"] == s["strike"] else "hedge"
        if leg not in k:                                        # the first row of a ts wins (the exit minute is snapped twice)
            m = _mid(r)
            if m is not None:
                k[leg] = m
    opened = _opened(t)
    out = []
    for ts in sorted(by):
        k = by[ts]
        at = _dt(ts)
        if "sold" in k and "hedge" in k and (opened is None or at > opened):
            out.append({"ts": ts, "at": at, "sold": k["sold"], "hedge": k["hedge"]})
    return out


def _net(t: dict, sold_px: float, hedge_px: float) -> float:
    qty = int(t["qty"])
    ch_in = charges(t["sold"]["entry"] * qty, sell=True) + charges(t["hedge"]["entry"] * qty, sell=False)
    ch_out = charges(sold_px * qty, sell=False) + charges(hedge_px * qty, sell=True)
    return round((t["credit"] - round(sold_px - hedge_px, 2)) * qty - ch_in - ch_out, 2)


def replay(t: dict, snaps: list[dict], rule: dict, config: AppConfig) -> dict:
    """One trade under one rule: {why, net, exit_t, thin}. `why` is premium / breakeven / target / vwap / time."""
    c = config.options
    sold_in, credit = float(t["sold"]["entry"]), float(t["credit"])
    expiry_day = (t.get("sessions") or 0) == 0 if t.get("sessions") is not None else False
    pct = rule["stop_pct_expiry"] if expiry_day else rule["stop_pct"]
    stop = round(sold_in * (1 + pct / 100), 2)
    be_at = round(sold_in * (1 - c.be_after_pct / 100), 2)
    target = round(credit * (1 - c.target_pct / 100), 2)
    t_exit = bs.hm(c.time_exit)
    real_vwap = rule["vwap"] and t.get("why") == "vwap stop" and _closed(t) is not None and t["sold"].get("exit") is not None
    vwap_at = _closed(t) if real_vwap else None
    be, last_at, gap, last = False, _opened(t), 0.0, None

    def done(why, sold_mid, hedge_mid, at):
        sold_px, hedge_px = round(sold_mid + 1.0, 2), round(max(0.05, hedge_mid - 1.0), 2)
        return {"why": why, "net": _net(t, sold_px, hedge_px), "exit_t": ampm(at), "thin": gap > THIN_GAP_MIN}

    for p in snaps:
        if vwap_at is not None and p["at"] > vwap_at:           # the real VWAP decision, at its real time and fills
            return {"why": "vwap", "net": _net(t, float(t["sold"]["exit"]), float(t["hedge"]["exit"])), "exit_t": ampm(vwap_at), "thin": gap > THIN_GAP_MIN}
        if last_at is not None:
            gap = max(gap, (p["at"] - last_at).total_seconds() / 60)
        last_at, last = p["at"], p
        if p["at"].time() >= t_exit:
            return done("time", p["sold"], p["hedge"], p["at"])
        value = p["sold"] - p["hedge"]
        if p["sold"] <= be_at:
            be = True
        if p["sold"] >= stop:
            return done("premium", p["sold"], p["hedge"], p["at"])
        if be and value >= credit:
            return done("breakeven", p["sold"], p["hedge"], p["at"])
        if value <= target:
            return done("target", p["sold"], p["hedge"], p["at"])
    if vwap_at is not None:
        return {"why": "vwap", "net": _net(t, float(t["sold"]["exit"]), float(t["hedge"]["exit"])), "exit_t": ampm(vwap_at), "thin": gap > THIN_GAP_MIN}
    if last is None:
        return {"why": None, "net": None, "exit_t": None, "thin": True}
    r = done("time", last["sold"], last["hedge"], last["at"])     # the snapshots ran out before the time exit: the last one seen
    r["thin"] = True
    return r


def _trades(since: str) -> list[dict]:
    with get_db() as conn:
        return [{**json.loads(r[1]), "id": r[0]} for r in conn.execute("SELECT id, payload FROM options_trades WHERE day >= ? ORDER BY day, id", (since,))]


def report(config: AppConfig, days: int = 60, today: Optional[date] = None) -> dict:
    """GET /api/options/asif: the rules side by side over the closed trades of the last `days` days."""
    today = today or datetime.now(IST).date()
    since = (today - timedelta(days=int(days))).isoformat()
    rs = rules(config)
    trades = _trades(since)
    per, sums = [], {r["key"]: [] for r in rs}
    skipped = thin = 0
    for t in trades:
        row = {"id": t["id"], "day": t["day"], "bias": t.get("bias"), "strikes": None, "actual": {"why": WHY.get(t.get("why"), t.get("why")), "label": t.get("label") or t.get("why"),
               "net": round(float(t.get("net") or 0.0), 2)}, "by_rule": {}, "skipped": None, "thin": False}
        if not (t.get("sold") and t.get("hedge") and t.get("qty") and t.get("credit") is not None and t.get("expiry")):
            row["skipped"] = "no legs recorded"
        else:
            row["strikes"] = f"{t['sold']['strike']:,.0f} {t['sold']['type']} / {t['hedge']['strike']:,.0f} {t['hedge']['type']}"
            snaps = path(t)
            if not snaps:
                row["skipped"] = "no snapshots that day"
            else:
                for r in rs:
                    res = replay(t, snaps, r, config)
                    row["by_rule"][r["key"]] = {k: res[k] for k in ("why", "net", "exit_t")}
                    row["thin"] = row["thin"] or res["thin"]
                    sums[r["key"]].append(res)
        skipped += bool(row["skipped"])
        thin += bool(row["thin"]) and not row["skipped"]
        per.append(row)
    out_rules = []
    for r in rs:
        xs = [x for x in sums[r["key"]] if x["net"] is not None]
        nets = [x["net"] for x in xs]
        out_rules.append({"key": r["key"], "label": r["label"], "stop_pct": r["stop_pct"], "stop_pct_expiry": r["stop_pct_expiry"], "vwap": r["vwap"],
                          "actual": bool(r.get("actual")), "trades": len(xs), "won": sum(1 for n in nets if n > 0), "net": round(sum(nets), 2),
                          "avg": round(sum(nets) / len(nets), 2) if nets else None, "worst": round(min(nets), 2) if nets else None,
                          "exits": {k: sum(1 for x in xs if x["why"] == k) for k in ("premium", "breakeven", "target", "vwap", "time")}})
    used = len(trades) - skipped
    note = NOTE + (f" · {skipped} trade{'s' if skipped != 1 else ''} skipped (no snapshots that day or no legs)" if skipped else "") \
        + (f" · {thin} with thin snapshots (a gap over {THIN_GAP_MIN} min: after a real stop only the bias snapshots to 11:00 AM and the close remain)" if thin else "")
    return {"from": since, "to": today.isoformat(), "days": int(days), "trades": len(trades), "replayed": used, "skipped": skipped,
            "snapshots_ok": used - thin, "thin": thin, "rules": out_rules, "per_trade": list(reversed(per)), "note": note,
            "time_exit": ampm(config.options.time_exit)}
