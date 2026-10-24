"""Telegram message text (HTML parse mode). Every value is escaped."""

from datetime import datetime
from html import escape
from typing import Optional

ICONS = {"breakout": "🚀", "breakout_low_volume": "⚠️", "below_pivot": "🔻", "played_out": "🏁",
         "forming": "🔵", "left": "▫️", "climbing": "📈", "near_pivot": "🎯", "intraday": "⚡",
         "system": "⚙️", "digest": "📋"}
SCREEN_NAMES = {"vcp": "VCP", "bases": "Bases"}


def rs(v: Optional[float]) -> str:
    return "n/a" if v is None else f"₹{v:,.2f}"


def _pct(v: Optional[float]) -> str:
    return "" if v is None else f" {v:+.1f}%"


def _tail(p: dict) -> str:
    bits = []
    if p.get("rs") is not None:
        bits.append(f"RS {p['rs']}")
    if p.get("tt_of"):
        bits.append(f"Trend {p['tt_passed']}/{p['tt_of']}")
    if p.get("industry"):
        bits.append(escape(p["industry"]))
    return " · ".join(bits)


def stage_alert(kind: str, screen: str, text: str, p: Optional[dict], close_in_range: Optional[float] = None,
                ctx: Optional[dict] = None) -> str:
    """One stage-move alert: the feed sentence plus key numbers; with a decision context,
    the Verdict / Why / Caution / Levels / Watch lines that match the chart."""
    head = f"{ICONS.get(kind, '•')} <b>{escape(p.get('symbol') if p else '')}</b> · {SCREEN_NAMES.get(screen, screen)}"
    lines = [head, escape(text)]
    if p:
        v = p.get("vcp") or {}
        if kind in ("breakout", "breakout_low_volume"):
            extra = []
            if close_in_range is not None:
                extra.append(f"close-in-range {close_in_range:.2f}")
            if v.get("stop"):
                risk = (p["close"] - v["stop"]) / p["close"] * 100
                extra.append(f"stop {rs(v['stop'])} ({risk:.1f}% risk from close)")
            if extra:
                lines.append(" · ".join(extra))
        if kind in ("forming", "near_pivot") and v.get("contractions"):
            lines.append("Pullbacks " + " → ".join(f"{c['depth_pct']:.0f}%" for c in v["contractions"])
                         + (f" · stop {rs(v['stop'])}" if v.get("stop") else ""))
        tail = _tail(p)
        if tail:
            lines.append(f"<i>{tail}</i>")
    if ctx:
        from backend.alerts.decision import decision_lines
        lines.append("")
        lines += [escape(x) for x in decision_lines(ctx)]
    return "\n".join(lines)


def near_pivot_alert(screen: str, p: dict, ctx: Optional[dict] = None) -> str:
    text = (f"{p['symbol']} is {p['dist_to_pivot_pct']:.1f}% under its {rs(p['pivot'])} pivot "
            f"({p['base_label']}). A close above it is a breakout.")
    return stage_alert("near_pivot", screen, text, p, ctx=ctx)


def intraday_alert(p: dict, ltp: float, vol_so_far: Optional[float], pace: Optional[float],
                   avg_vol: Optional[float], ctx: Optional[dict] = None) -> str:
    lines = [f"{ICONS['intraday']} <b>{escape(p['symbol'])}</b> trading {rs(ltp)}, "
             f"{(ltp / p['pivot'] - 1) * 100:+.1f}% above its {rs(p['pivot'])} pivot ({escape(p['base_label'])})",
             "<b>Unconfirmed</b>: a breakout counts only on a close above the pivot."]
    if vol_so_far is not None and avg_vol:
        lines.append(f"Volume so far {vol_so_far / avg_vol:.1f}× the 50-day average"
                     + (f", on pace for {pace:.1f}×" if pace else ""))
    tail = _tail(p)
    if tail:
        lines.append(f"<i>{tail}</i>")
    if ctx:
        from backend.alerts.decision import decision_lines
        lines.append("")
        lines += [escape(x) for x in decision_lines(ctx)]
    return "\n".join(lines)


def digest(screen: str, as_of: str, counts: dict, moves: list[dict], fresh_sessions: int = 5) -> list[str]:
    """Daily digest split into Telegram-sized chunks (< 4,000 chars each)."""
    c = counts or {}
    head = (f"{ICONS['digest']} <b>{SCREEN_NAMES.get(screen, screen)} screen · EOD {as_of}</b>\n"
            f"{c.get('fresh', 0)} fresh breakouts in the last {fresh_sessions} sessions\n"
            f"Forming {c.get('forming', 0)} · Fresh {c.get('fresh', 0)} · Climbing {c.get('climbing', 0)}"
            f" · Played out {c.get('played_out', 0)}"
            + (f" · low-volume breakouts {c['fresh_low_volume']}" if c.get("fresh_low_volume") else "")
            + f"\n\n<b>What changed since last close</b> ({len(moves)} moves)")
    lines = [f"{ICONS.get(m['kind'], '•')} {escape(m['text'])} <i>{rs(m['close'])}{_pct(m.get('change_pct'))}</i>"
             for m in moves] or ["No stage moves today."]
    chunks, cur = [], head
    for ln in lines:
        if len(cur) + len(ln) + 2 > 3900:
            chunks.append(cur)
            cur = "(continued)"
        cur += "\n" + ln
    chunks.append(cur)
    return chunks


def system_alert(text: str) -> str:
    return f"{ICONS['system']} {escape(text)}"


LOGIN_WHY = {
    "new_day": "New Kite day: yesterday's session ended at 06:00.",
    "still_needed": "Still not logged in. Intraday alerts start 09:20; the evening scan needs it too.",
    "evening": "Kite login needed for today's evening scan.",
    "revoked": "Kite rejected the current session (logged out or revoked).",
    "requested": "Login link as requested.",
}


def login_link_alert(reason: str, expires: datetime) -> str:
    return (f"🔑 <b>Kite login needed</b>\n{escape(LOGIN_WHY.get(reason, reason))}\n"
            f"Tap <b>Log in to Kite</b>: works once, until {expires:%H:%M}. "
            "Gives a fresh session until 06:00 tomorrow (Banana Patterns + AlgoTrade).")


def login_heads_up(first_slot: str) -> str:
    return f"⏰ <b>Kite session ends at 06:00</b>\nYour login link comes at {escape(first_slot)}."


def login_ok_alert(expires: datetime, handover: dict) -> str:
    lines = [f"✅ <b>Kite connected</b> · valid until {expires:%H:%M %d %b}"]
    if handover.get("configured"):
        lines.append("AlgoTrade: ✓ token updated" if handover["ok"]
                     else f"AlgoTrade: ✗ not updated ({escape(handover['message'])})")
    return "\n".join(lines)
