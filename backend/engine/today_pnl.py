"""₹ Today: the status bar's "today's P&L" chip (stream topic `today_pnl`). Cheap and read-only: stored rows and stored
last prices only (no quote call, no broker call).

  kite   the Tips → Kite realised today (kite_orders.realised_today, live rows) + the open P&L of every open Kite
         position at its last stored quote. Mode dry: shown as "dry", never added to the total (not money). Mode off:
         the part is left out.
  paper  the Tips paper book's today (trades closed today + the open positions), the "₹x today" of the Tips · paper
         card. Shown, never added to the total (not money).
  real   the Real stock book's day: its value now (stored last prices) against the last equity row before today,
         money added / withdrawn today taken out (realised + open).
  total  kite (live only) + real. Hidden before 9:00 AM, and when every part is zero and nothing was traded today."""

from datetime import datetime, time
from typing import Optional

from backend.config import IST
from backend.database.models import get_db

SHOW_FROM = time(9, 0)


def kite_part(config, now: datetime) -> Optional[dict]:
    from backend.tips import book as bk
    from backend.tips import card as cd
    from backend.tips import kite_orders as ko
    m = ko.mode(config)
    if m == "off":
        return None
    dry = m == "dry"
    day = now.date().isoformat()
    realised = ko.realised_today(day, dry)
    open_sum, n_open = 0.0, 0
    for h in bk.rows("json_extract(kite, '$.state') = 'entered'"):
        k = ko.kstate(h)
        if bool(k.get("dry")) != dry or int(k.get("qty_open") or 0) <= 0:
            continue
        n_open += 1
        epx, ltp = k.get("entry_px"), bk.last_ltp(h)
        if epx is not None and ltp is not None:                   # the open part only: the booked parts are in realised
            open_sum += (float(ltp) - float(epx)) * int(k["qty_open"]) * cd._sign(h)
    with get_db() as conn:
        traded = conn.execute("SELECT COUNT(*) FROM tips_orders WHERE day = ? AND leg != 'test' AND status NOT IN ('voided', 'failed', 'rejected')",
                              (day,)).fetchone()[0]
    return {"pnl": round(realised + open_sum, 2), "realised": round(realised, 2), "open": round(open_sum, 2), "dry": dry,
            "label": "dry" if dry else "Kite", "n_open": n_open, "traded": bool(traded)}


def paper_part(config, now: datetime) -> dict:
    from backend.tips import book as bk
    v = bk.view(config, now)
    day = now.date().isoformat()
    closed = [t for t in v["trades"] if (t.get("closed_at") or "")[:10] == day]
    return {"pnl": round(sum(t["net"] or 0 for t in closed) + v["open_pnl"], 2), "n_open": len(v["positions"]),
            "traded": bool(closed) or bool(v["positions"]), "label": "paper"}


def real_part(now: datetime) -> Optional[dict]:
    from backend.engine import real
    st = real._state()
    if not st:
        return None
    day = now.date().isoformat()
    with get_db() as conn:
        prev = conn.execute("SELECT equity FROM real_equity WHERE day < ? ORDER BY day DESC LIMIT 1", (day,)).fetchone()
        traded = conn.execute("SELECT COUNT(*) FROM real_orders WHERE day = ? AND status = 'sent'", (day,)).fetchone()[0]
    added = sum(float(x.get("amount") or 0) for x in st.get("deposits") or [] if str(x.get("at") or "")[:10] == day)
    pnl = round(real._value(st) - float(prev[0]) - added, 2) if prev else 0.0
    return {"pnl": pnl, "n_open": len(st.get("positions") or {}), "traded": bool(traded), "label": "real"}


def today_pnl(config, now: Optional[datetime] = None) -> dict:
    """{visible, total, total_text, parts [{key, label, pnl, text, counted}], open, title, at}."""
    from backend.tips import book as bk
    now = now or datetime.now(IST)
    parts = []
    k = kite_part(config, now)
    if k is not None:
        parts.append({"key": "kite", **k, "counted": not k["dry"]})
    p = paper_part(config, now)
    parts.append({"key": "paper", **p, "counted": False})
    r = real_part(now)
    parts.append({"key": "real", **(r or {"pnl": 0.0, "n_open": 0, "traded": False, "label": "real"}), "counted": True})
    for x in parts:
        x["text"] = f"{x['label']} {bk.inr(x['pnl'], sign=True) if x['pnl'] else bk.inr(0)}"
    total = round(sum(x["pnl"] for x in parts if x["counted"]), 2)
    anything = any(x["pnl"] for x in parts) or any(x["traded"] for x in parts)
    visible = now.time() >= SHOW_FROM and bool(anything)
    return {"visible": visible, "total": total, "total_text": bk.inr(total, sign=True) if total else bk.inr(0),
            "tone": "up" if total > 0 else "down" if total < 0 else "", "parts": parts,
            "open": any(x.get("n_open") for x in parts),
            "title": "₹ Today: Kite (live) + real · paper is not counted in the total" + (" · a dry run is not counted either" if k and k["dry"] else ""),
            "at": now.isoformat(timespec="seconds")}
