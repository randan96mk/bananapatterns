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


def digest(screen: str, as_of: str, counts: dict, moves: list[dict], fresh_sessions: int = 5,
           market: Optional[str] = None) -> list[str]:
    """Daily digest split into Telegram-sized chunks (< 4,000 chars each). market: market_block() HTML;
    a move's "tier" (A/B) tags buy-side lines."""
    c = counts or {}
    head = (f"{ICONS['digest']} <b>{SCREEN_NAMES.get(screen, screen)} screen · EOD {as_of}</b>\n"
            f"{c.get('fresh', 0)} fresh breakouts in the last {fresh_sessions} sessions\n"
            f"Forming {c.get('forming', 0)} · Fresh {c.get('fresh', 0)} · Climbing {c.get('climbing', 0)}"
            f" · Played out {c.get('played_out', 0)}"
            + (f" · low-volume breakouts {c['fresh_low_volume']}" if c.get("fresh_low_volume") else "")
            + (f"\n\n{market}" if market else "")
            + f"\n\n<b>What changed since last close</b> ({len(moves)} moves)")
    tag = {"A": "🅰️ ", "B": "🅱️ "}
    lines = [f"{tag.get(m.get('tier'), '')}{ICONS.get(m['kind'], '•')} {escape(m['text'])} "
             f"<i>{rs(m['close'])}{_pct(m.get('change_pct'))}</i>" for m in moves] or ["No stage moves today."]
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
        names = [f"{'🅱️ ' if i.get('tier') == 'B' else ''}{escape(i['symbol'])} "
                 f"({(i['ltp'] / i['pivot'] - 1) * 100:+.1f}%, {_pace(i.get('pace'))})"
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


STATUS_TEXT = {"uptrend": "🟢 Uptrend", "pressure": "🟡 Under pressure", "correction": "🔴 Correction"}


def market_line(h: dict, index_today: Optional[float] = None) -> str:
    """One line for the top of an alert."""
    bits = [f"Market {STATUS_TEXT.get(h['status'], h['status'])}"]
    br, dd, bo = h["breadth"].get("above_50_pct"), h.get("dist_days"), h["breakouts"]
    if br is not None:
        bits.append(f"breadth {br:.0f}%")
    if dd is not None:
        bits.append(f"{dd} dist. days")
    if bo.get("total"):
        bits.append(f"breakouts working {bo['working']}/{bo['total']}")
    if index_today is not None:
        bits.append(f"Nifty 500 {index_today:+.1f}% today")
    return "<i>" + " · ".join(bits) + "</i>"


def market_block(h: dict, prev: Optional[dict] = None) -> str:
    """Fuller market section for the digest."""
    ok = lambda v: "✓" if v else ("✗" if v is False else "n/a")  # noqa: E731
    ix, br, bo = h["index"], h["breadth"], h["breakouts"]
    head = f"📊 <b>Market {STATUS_TEXT.get(h['status'], h['status'])}</b>"
    if prev and prev.get("status") != h["status"]:
        head += f" (was {STATUS_TEXT.get(prev['status'], prev['status'])})"
    lines = [head,
             f"Nifty 500: above 50-day {ok(ix.get('above_50'))} · above 200-day {ok(ix.get('above_200'))}"
             f" · 50-day rising {ok(ix.get('ma50_rising'))}"
             + (f" · {ix['change_pct']:+.1f}% today" if ix.get("change_pct") is not None else "")]
    if br.get("above_50_pct") is not None:
        lines.append(f"Breadth: {br['above_50_pct']:.0f}% above the 50-day"
                     + (f", {br['above_200_pct']:.0f}% above the 200-day" if br.get("above_200_pct") is not None else "")
                     + f" · 52-week highs {br['new_highs']} / lows {br['new_lows']}")
    if h.get("dist_days") is not None:
        lines.append(f"Distribution days: {h['dist_days']} in the last {h.get('windows', {}).get('dist', 25)} sessions")
    if bo.get("total"):
        lines.append(f"Breakouts working: {bo['working']}/{bo['total']} from the last "
                     f"{h.get('windows', {}).get('breakouts', 20)} sessions")
    return "\n".join(lines)


def _coil_text(c: Optional[dict]) -> str:
    if not c:
        return ""
    day = "NR7" if c.get("nr7") else "inside day"
    return f"🌀 coiled ({day}, volume {c['volume_x']:.2f}×)"


def _plan_line(n: int, i: dict) -> str:
    tag = {"A": "🅰️ ", "B": "🅱️ "}.get(i.get("tier"), "")
    bits = [f"{n}. {tag}<b>{escape(i['symbol'])}</b> {escape(i.get('base_label') or '')}: close above "
            f"<b>{rs(i['pivot'])}</b> ({i['dist_to_pivot_pct']:.1f}% away) · buy zone to {rs(i['zone_top'])} · "
            f"stop {rs(i['stop'])}"]
    if i.get("size"):
        bits.append(f" · ~{i['size']['qty']} sh")
    coil = _coil_text(i.get("coiled"))
    return "".join(bits) + (f"\n    {coil}" if coil else "")


def top_setups(items: list[dict], as_of: str) -> str:
    lines = [f"🎯 <b>Top {len(items)} setups for the next session</b> (scan {escape(as_of)})"]
    lines += [_plan_line(n, i) for n, i in enumerate(items, 1)]
    lines.append("<i>A breakout counts on a close above the pivot on ≥ 1.5× volume. Charts follow.</i>")
    return "\n".join(lines)


def top_setup_caption(n: int, i: dict, ctx: Optional[dict] = None) -> str:
    lines = [_plan_line(n, i)]
    if ctx:
        from backend.alerts.decision import decision_lines
        lines += [escape(x) for x in decision_lines(ctx)]
    return "\n".join(lines)


def morning_plan(items: list[dict], plan_as_of: str, today: str) -> str:
    lines = [f"☀️ <b>Today's plan</b> ({escape(today)}, from the {escape(plan_as_of)} scan)"]
    if not items:
        lines.append("No setups near a pivot today. Patience is a position.")
    lines += [_plan_line(n, i) for n, i in enumerate(items, 1)]
    lines.append("<i>Heads-up alerts from 09:20 when a name trades above its pivot; the 15:15 check says whether "
                 "it's holding into the close.</i>")
    return "\n".join(lines)


GAP_TEXT = {"chase": "⛔ <b>{sym}</b> opening {rs_open}, {pct:+.1f}% over the pivot: beyond the buy zone. Don't chase.",
            "above": "🟢 <b>{sym}</b> opening {rs_open}, {pct:+.1f}% over the pivot: watch whether it holds.",
            "broken": "🔻 <b>{sym}</b> opening {rs_open}, below the stop {stop}: setup broken, skip it."}


def gap_check(results: list[tuple[dict, float, str]]) -> str:
    lines = ["🔔 <b>Opening check</b> (plan names)"]
    for i, open_px, verdict in results:
        lines.append(GAP_TEXT[verdict].format(sym=escape(i["symbol"]), rs_open=rs(open_px),
                                              pct=(open_px / i["pivot"] - 1) * 100, stop=rs(i["stop"])))
    return "\n".join(lines)
