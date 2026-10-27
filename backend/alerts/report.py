"""Evening report (summary alert style): the evening's alerts, held in alert_outbox by the evening job, sent as one
message after the top setups are ranked, one line per stock, with buttons for the charts (one album) and the full
texts. Sent silently: only "act now" alerts during market hours make a sound."""

import json
import uuid
from datetime import date
from typing import Optional

from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier, split_caption, strip_tags
from backend.database.models import get_db
from backend.logging_config.logger import setup_logger

logger = setup_logger("report")

TIER = {"A": "🅰️", "B": "🅱️"}
MAX_NAMES = 12                                          # names listed per section; the rest is "+N more"
SECTIONS = (                                            # (kinds, icon, title); breakouts and near pivot get full lines
    (("below_pivot",), "🔻", "Back under the pivot"),
    (("played_out",), "🏁", "Played out"),
    (("climbing",), "↩️", "Back over the pivot"),
    (("forming",), "🟡", "New bases forming"),
    (("left",), "🚪", "Left the screens"),
)
BREAKOUTS = ("breakout", "breakout_low_volume")


def held(batch: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT key, kind, symbol, html, meta FROM alert_outbox WHERE batch = ? ORDER BY rowid",
                            (batch,)).fetchall()
    return [dict(r, meta=json.loads(r["meta"] or "{}")) for r in rows]


def _day(d: str) -> str:
    x = date.fromisoformat(d)
    return f"{x.day} {x:%b}"


def _names(rows: list[dict], extra=lambda r: "") -> str:
    seen, out = set(), []
    for r in rows:
        if r["symbol"] and r["symbol"] not in seen:
            seen.add(r["symbol"])
            out.append(f"{msg.escape(r['symbol'])}{extra(r)}")
    more = len(out) - MAX_NAMES
    return " · ".join(out[:MAX_NAMES]) + (f" · +{more} more" if more > 0 else "")


def px(v: float) -> str:
    """Whole rupees from Rs 100 up (a one-line-per-stock report); the full alerts keep the paise."""
    return f"₹{v:,.0f}" if v >= 100 else msg.rs(v)


def _first_line(html: str) -> str:
    return msg.escape(strip_tags(html.split("\n")[0])) if html else ""


def breakout_line(r: dict) -> str:
    m = r["meta"]
    bits = [f"<b>{msg.escape(r['symbol'])}</b>"]
    if m.get("change_pct") is not None:
        bits.append(f"{m['change_pct']:+.1f}%")
    if m.get("close"):
        bits.append(px(m["close"]))
    label = " ".join(x for x in (m.get("label") or "", TIER.get(m.get("tier"), "")) if x)
    if label:
        bits.append(msg.escape(label))
    if m.get("stop") and m.get("close"):
        bits.append(f"stop {(1 - m['stop'] / m['close']) * 100:.1f}%")
    if r["kind"] == "breakout_low_volume":
        bits.append("⚠️ low volume")
    return " · ".join(bits)


def plan_line(n: int, i: dict) -> str:
    bits = [f"{n} {TIER.get(i.get('tier'), '')} <b>{msg.escape(i['symbol'])}</b> {px(i['pivot'])}",
            f"{i['dist_to_pivot_pct']:.1f}% away"]
    if i.get("stop"):
        bits.append(f"stop {px(i['stop'])}")
    if i.get("results"):
        bits.append("⚠️ results")
    return " · ".join(bits)


def build(as_of: str, items: list[dict], market: Optional[str] = None,
          orb: Optional[list[str]] = None, batch: Optional[str] = None) -> tuple[str, list]:
    """(html, buttons) of the evening report for the scan `as_of`; items = the top setups (plan); batch = the held
    alerts to use (default: as_of; the dashboard's sample uses its own)."""
    rows = held(batch or as_of)
    by_kind: dict = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    lines = [f"🌙 <b>Evening report</b> · scan {_day(as_of)}"]
    if market:
        lines.append(market)
    brk = [r for k in BREAKOUTS for r in by_kind.get(k, [])]
    if brk:
        lines += ["", f"🚀 <b>Breakouts ({len(brk)})</b>"] + [breakout_line(r) for r in brk[:MAX_NAMES]]
        if len(brk) > MAX_NAMES:
            lines.append(f"+{len(brk) - MAX_NAMES} more (🧾 Details)")
    near = by_kind.get("near_pivot", [])
    if near:
        lines += ["", f"👀 <b>Near the pivot ({len(near)})</b>: " + _names(
            near, lambda r: f" {r['meta']['dist']:.1f}%" if r["meta"].get("dist") is not None else "")]
    for kinds, icon, title in SECTIONS:
        sec = [r for k in kinds for r in by_kind.get(k, [])]
        if sec:
            lines += ["", f"{icon} <b>{title} ({len(sec)})</b>: " + _names(sec)]
    guard = [r for r in by_kind.get("guard", []) if r["html"]]
    if guard:
        lines += ["", "💼 <b>Your positions</b>"] + [_first_line(r["html"]) for r in guard[:8]]
    other = [r for r in by_kind.get("intraday", [])]
    if other:
        lines += ["", "⚠️ <b>Not confirmed at the close</b>: " + _names(other)]
    if items:
        lines += ["", f"🎯 <b>Top {len(items)} for the next session</b>"] + [plan_line(n, i)
                                                                        for n, i in enumerate(items, 1)]
    if orb:
        lines += ["", f"🚀 <b>Momentum ORB list ({len(orb)})</b>: " + " · ".join(msg.escape(s) for s in orb[:MAX_NAMES])
                  + (f" · +{len(orb) - MAX_NAMES} more" if len(orb) > MAX_NAMES else "")]
    if len(lines) <= 2:
        lines += ["", "A quiet day: no stage changes and no setups near a pivot."]
    lines += ["", "<i>Charts and the full alerts are on the buttons.</i>"]
    buttons = []
    if brk:
        buttons.append((f"📈 Breakouts ({len(brk)})", None, f"r:b:{as_of}"))
    if items:
        buttons.append((f"📈 Top {len(items)}", None, f"r:t:{as_of}"))
    if rows:
        buttons.append(("🧾 Details", None, f"r:d:{as_of}"))
    html = "\n".join(lines)
    while len(html) > 4000 and len(lines) > 4:              # stay under Telegram's 4,096 characters
        lines.pop(-3)
        html = "\n".join(lines)
    return html, buttons


def send(notifier: Notifier, as_of: str, items: list[dict], market: Optional[str] = None,
         orb: Optional[list[str]] = None) -> bool:
    html, buttons = build(as_of, items, market, orb)
    with get_db() as conn:                                  # the buttons work for 30 days
        conn.execute("DELETE FROM alert_outbox WHERE batch < date(?, '-30 days')", (as_of,))
    return notifier.send(f"{as_of}|report", "report", html, buttons=buttons or None, silent=True)


# --- the buttons ------------------------------------------------------------------------------------

def _album(notifier: Notifier, config, entries: list[tuple[str, str]]) -> int:
    """Charts for [(symbol, caption html)] as one album (one chart: a photo), then one message of action buttons."""
    from backend.alerts.preview import alert_chart
    photos, syms = [], []
    for sym, cap in entries[:10]:
        try:
            png, _ = alert_chart(config, sym)
        except (LookupError, ValueError) as exc:
            logger.info("No chart for %s: %s", sym, exc)
            continue
        if png:
            photos.append((png, split_caption(cap)[0]))
            syms.append(sym)
    if not photos:
        return 0
    if len(photos) == 1:
        notifier.client.send_photo(notifier.chat_id, photos[0][0], photos[0][1], silent=True)
    else:
        notifier.client.send_album(notifier.chat_id, photos)
    rows = [[(f"📌 Watch {s}", None, f"w:{s}"), (f"✅ I bought {s}", None, f"b:{s}")] for s in syms]
    notifier.send(f"bot|{uuid.uuid4().hex}", "bot", "Act on a chart above:", buttons=rows, silent=True)
    return len(photos)


def tap(what: str, as_of: str, notifier: Notifier, config) -> str:
    """The report's buttons: b = breakout charts, t = top setup charts, d = the full alerts as text."""
    if what == "b":
        rows = [r for r in held(as_of) if r["kind"] in BREAKOUTS]
        n = _album(notifier, config, [(r["symbol"], r["html"]) for r in rows])
        return f"📈 {n} breakout chart{'s' if n != 1 else ''}" if n else "No charts to send."
    if what == "t":
        with get_db() as conn:
            row = conn.execute("SELECT payload FROM daily_plan WHERE as_of = ?", (as_of,)).fetchone()
        items = json.loads(row["payload"]) if row else []
        n = _album(notifier, config, [(i["symbol"], msg._plan_line(k, i)) for k, i in enumerate(items, 1)])
        return f"📈 {n} top setup chart{'s' if n != 1 else ''}" if n else "No charts to send."
    if what == "d":
        rows = held(as_of)
        rows.sort(key=lambda r: r["kind"] == "digest")      # the per-screen digests last
        chunks, cur = [], ""
        for r in rows:
            part = r["html"].strip()
            if cur and len(cur) + len(part) + 8 > 3900:
                chunks.append(cur)
                cur = ""
            cur = f"{cur}\n\n— — —\n\n{part}" if cur else part
        if cur:
            chunks.append(cur)
        for c in chunks:
            notifier.send(f"bot|{uuid.uuid4().hex}", "bot", c, silent=True)
        return f"🧾 {len(rows)} alerts in {len(chunks)} message{'s' if len(chunks) != 1 else ''}"
    return ""
