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


VOLUME_LINE = {"strong": "🟢 <b>Strong volume</b>: on pace for {pace}. A real breakout candidate.",
               "average": "🟡 <b>Average volume</b> ({pace}): wait for the {check} check.",
               "weak": "🟡 <b>Weak volume</b> ({pace}): wait for the {check} check.",
               "unknown": "Volume too early to judge: wait for the {check} check."}


def intraday_alert(p: dict, ltp: float, vol_so_far: Optional[float], pace: Optional[float],
                   avg_vol: Optional[float], ctx: Optional[dict] = None, volume: Optional[str] = None,
                   zone_top: Optional[float] = None, check_time: str = "15:15") -> str:
    lines = [f"{ICONS['intraday']} <b>{escape(p['symbol'])}</b> trading {rs(ltp)}, "
             f"{(ltp / p['pivot'] - 1) * 100:+.1f}% above its {rs(p['pivot'])} pivot ({escape(p['base_label'])})",
             "<b>Unconfirmed</b>: a breakout counts only on a close above the pivot."]
    if zone_top and ltp > zone_top:
        lines.append(f"⛔ <b>Extended</b>: beyond the buy zone ({rs(zone_top)}). Don't chase.")
    elif volume:
        lines.append(VOLUME_LINE[volume].format(pace=f"{pace:.1f}× average" if pace else "n/a",
                                                check=escape(check_time)))
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


def failed_alert(p: dict, ltp: float, day_high: Optional[float], stop: Optional[float]) -> str:
    lines = [f"⚠️ <b>{escape(p['symbol'])}: breakout failing.</b> Back at {rs(ltp)}, below the {rs(p['pivot'])} pivot"
             + (f" (today's high {rs(day_high)})." if day_high else "."),
             "Don't buy." + (f" If you bought, honour the stop {rs(stop)}." if stop else "")]
    tail = _tail(p)
    if tail:
        lines.append(f"<i>{tail}</i>")
    return "\n".join(lines)


CLOSE_HEAD = {"holding": "✅ <b>{sym}: breakout holding into the close</b>",
              "weak": "🟡 <b>{sym}: above the pivot, but weak</b>",
              "extended": "⛔ <b>{sym}: too far above the pivot</b>"}


def close_alert(p: dict, verdict: str, ltp: float, pace: Optional[float], rng: Optional[float],
                zone_top: float, ctx: Optional[dict] = None) -> str:
    lines = [CLOSE_HEAD[verdict].format(sym=escape(p["symbol"])),
             f"{rs(ltp)}, {(ltp / p['pivot'] - 1) * 100:+.1f}% over the {rs(p['pivot'])} pivot "
             f"({escape(p['base_label'])}) · volume on pace {_pace(pace)} · "
             f"range {f'{rng:.2f}' if rng is not None else 'n/a'}"]
    if verdict == "holding":
        lines.append(f"Buy zone {rs(p['pivot'])} – {rs(zone_top)} · act before 15:30. The 18:30 scan confirms it.")
    elif verdict == "weak":
        lines.append("Volume or the close may not confirm: wait for the 18:30 scan.")
    else:
        lines.append("Don't chase: wait for a pullback toward the pivot.")
    tail = _tail(p)
    if tail:
        lines.append(f"<i>{tail}</i>")
    if ctx:
        from backend.alerts.decision import decision_lines
        lines.append("")
        lines += [escape(x) for x in decision_lines(ctx)]
    return "\n".join(lines)


def _pace(pace: Optional[float]) -> str:
    return f"{pace:.1f}×" if pace else "n/a"


def close_summary(items: list[dict], shown: set) -> str:
    """One message for the 15:15 names not sent individually. items: {symbol, verdict, ltp, pivot, pace}."""
    groups = {"holding": "✅ Also holding", "weak": "🟡 Above pivot, weak", "extended": "⛔ Extended, don't chase"}
    lines = [f"🕒 <b>15:15 check</b>: {len(items)} names above their pivot"]
    for v, title in groups.items():
        names = [f"{escape(i['symbol'])} ({(i['ltp'] / i['pivot'] - 1) * 100:+.1f}%, {_pace(i.get('pace'))})"
                 for i in items if i["verdict"] == v and i["symbol"] not in shown]
        if names:
            lines.append(f"{title}: " + ", ".join(names))
    return "\n".join(lines)


def confirm_note(flag: Optional[str]) -> Optional[str]:
    """18:30 cross-reference to the 15:15 check."""
    return {"close_holding": "✓ Flagged at 15:15 (holding into the close)",
            "close_weak": "✓ Confirmed, though it looked weak at 15:15",
            "close_extended": "✓ Confirmed; it was extended at 15:15"}.get(flag)


def not_confirmed_alert(symbol: str, pivot: float, close: Optional[float]) -> str:
    return (f"✗ <b>{escape(symbol)}</b> didn't confirm on the close: flagged as holding at 15:15, "
            f"but the close {rs(close)} isn't a volume-confirmed breakout over the {rs(pivot)} pivot.")
