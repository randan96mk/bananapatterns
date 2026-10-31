"""Telegram message text (HTML parse mode). Every value is escaped."""

from datetime import datetime
from html import escape
from typing import Optional

from backend.timefmt import ampm, ampm_date

ICONS = {"breakout": "🚀", "breakout_low_volume": "⚠️", "below_pivot": "🔻", "played_out": "🏁",
         "forming": "🔵", "left": "▫️", "climbing": "📈", "near_pivot": "🎯", "intraday": "⚡",
         "system": "⚙️", "digest": "📋"}
SCREEN_NAMES = {"vcp": "VCP", "cup": "Cup &amp; handle", "flag": "Bull flag", "bases": "Bases"}
SCREEN_PREF = {"vcp": 0, "cup": 1, "flag": 2, "bases": 3}   # a name on several screens: the most specific one
SCREEN_ORDER_SQL = "CASE {col} WHEN 'vcp' THEN 0 WHEN 'cup' THEN 1 WHEN 'flag' THEN 2 ELSE 3 END"


def setup_stop(p: dict) -> Optional[float]:
    """The setup's own stop: the VCP stop, else the cup's (handle low - buffer), else the flag's (flag low -
    buffer), else None."""
    return (p.get("vcp") or {}).get("stop") or (p.get("cup") or {}).get("stop") or (p.get("flag") or {}).get("stop")


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
        v, cup, flag, stop = p.get("vcp") or {}, p.get("cup"), p.get("flag"), setup_stop(p)
        if kind in ("breakout", "breakout_low_volume"):
            extra = []
            if close_in_range is not None:
                extra.append(f"close-in-range {close_in_range:.2f}")
            if stop:
                risk = (p["close"] - stop) / p["close"] * 100
                extra.append(f"stop {rs(stop)} ({risk:.1f}% risk from close)")
            if extra:
                lines.append(" · ".join(extra))
        if kind in ("forming", "near_pivot") and v.get("contractions"):
            lines.append("Pullbacks " + " → ".join(f"{c['depth_pct']:.0f}%" for c in v["contractions"])
                         + (f" · stop {rs(v['stop'])}" if v.get("stop") else ""))
        if kind in ("forming", "near_pivot") and cup:
            h, c = cup["handle"], cup["cup"]
            lines.append(f"Cup {c['depth_pct']:.0f}% · {c['weeks']}w · handle {h['depth_pct']:.1f}%"
                         + (f" · vol {h['volume_x']:.2f}×" if h.get("volume_x") is not None else "")
                         + f" · stop {rs(cup['stop'])}")
        if kind in ("forming", "near_pivot") and flag:
            pl_, fl_ = flag["pole"], flag["flag"]
            tags = " · ".join(t for t, on in (("HTF", flag.get("htf")), ("tight", flag.get("tight")),
                                              ("post-results", flag.get("post_results"))) if on)
            lines.append(f"Pole +{pl_['gain_pct']:.0f}% · {pl_['bars']}d · flag −{fl_['depth_pct']:.1f}% · "
                         f"{fl_['bars']}d" + (f" · vol {fl_['volume_x']:.2f}×" if fl_.get("volume_x") is not None else "")
                         + f" · stop {rs(flag['stop'])}" + (f" · {tags}" if tags else ""))
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
                   zone_top: Optional[float] = None, check_time: str = "15:15", ck: Optional[dict] = None,
                   size: Optional[dict] = None, stop: Optional[float] = None, size_note: str = "") -> str:
    lines = [f"{ICONS['intraday']} <b>{escape(p['symbol'])}</b> trading {rs(ltp)}, "
             f"{(ltp / p['pivot'] - 1) * 100:+.1f}% above its {rs(p['pivot'])} pivot ({escape(p['base_label'])})",
             "<b>Unconfirmed</b>: a breakout counts only on a close above the pivot."]
    if zone_top and ltp > zone_top:
        lines.append(f"⛔ <b>Extended</b>: beyond the buy zone ({rs(zone_top)}). Don't chase.")
    elif volume:
        lines.append(VOLUME_LINE[volume].format(pace=f"{pace:.1f}× average" if pace else "n/a",
                                                check=_when(check_time)))
    if vol_so_far is not None and avg_vol:
        lines.append(f"Volume so far {vol_so_far / avg_vol:.1f}× the 50-day average"
                     + (f", on pace for {pace:.1f}×" if pace else ""))
    if ck and ck["verdict"]:
        lines += checklist_lines(ck)
        if ck["verdict"] == "valid":
            lines.append(action_line(ck, stop, size, size_note))
        elif ck["verdict"] == "wait":
            lines.append("<i>You'll get a note if it turns green today.</i>")
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


def _when(t: str) -> str:
    """'15:15' -> '3:15 PM'; words such as 'evening' pass through."""
    return ampm(t) if ":" in str(t) else escape(str(t))


def system_alert(text: str) -> str:
    return f"{ICONS['system']} {escape(text)}"


LOGIN_WHY = {
    "new_day": "New Kite day: yesterday's session ended at 6:00 AM IST.",
    "still_needed": "Still not logged in. Intraday alerts start at {start}; the evening scan needs it too.",
    "evening": "Kite login needed for today's evening scan.",
    "revoked": "Kite rejected the current session (logged out or revoked).",
    "requested": "Login link as requested.",
}


def login_link_alert(reason: str, expires: datetime, intraday_start: str = "09:20") -> str:
    why = LOGIN_WHY.get(reason, reason).replace("{start}", ampm(intraday_start))
    return (f"🔑 <b>Kite login needed</b>\n{escape(why)}\n"
            f"Tap <b>Log in to Kite</b>: works once, until {ampm(expires)}. "
            "Gives a fresh session until 6:00 AM IST tomorrow.")


def login_heads_up(first_slot: str) -> str:
    return f"⏰ <b>Kite session ends at 6:00 AM IST</b>\nYour login link comes at {ampm(first_slot)}."


def login_ok_alert(expires: datetime) -> str:
    return f"✅ <b>Kite connected</b> · valid until {ampm_date(expires)} IST"


VERDICT = {"valid": "🟢 <b>Valid entry</b>", "wait": "🟡 <b>Wait</b>", "skip": "🔴 <b>Skip</b>"}


def checklist_lines(ck: dict) -> list[str]:
    """'🟡 Wait · 3 of 5' and one ✅ / ❌ / ⏳ line per check (live.checklist)."""
    head = f"{VERDICT[ck['verdict']]} · {ck['passed']} of {ck['of']}"
    if ck["verdict"] == "skip":
        head += f": {escape(ck['reason'])}"
    return [head] + [f"{'✅' if ok else '⏳' if ok is None else '❌'} {escape(text)}" for _, ok, text in ck["checks"]]


def action_line(ck: dict, stop: Optional[float], size: Optional[dict], size_note: str = "") -> str:
    """🎯 Buy up to (top of the zone) · stop · qty and ₹ at risk (qty only when capital is set)."""
    line = f"🎯 <b>Buy up to {rs(ck['zone_top'])}</b> (top of the buy zone)"
    if stop:
        line += f" · stop {rs(stop)}"
    if size and size["qty"] > 0:
        line += f"\n<b>Qty {size['qty']}</b> · risk ₹{size['risk_rs']:,}" + (f" ({escape(size_note)})" if size_note else "")
    return line


def entry_upgrade(p: dict, ltp: float, ck: dict, stop: Optional[float], size: Optional[dict],
                  size_note: str = "") -> str:
    """⬆️ a 🟡 Wait name that is now 🟢 valid."""
    return "\n".join([f"⬆️ <b>{escape(p['symbol'])}</b> now a 🟢 <b>valid entry</b> at {rs(ltp)} "
                      f"({ck['ext_pct']:+.1f}% over the {rs(p['pivot'])} pivot, unconfirmed until the close)"]
                     + checklist_lines(ck)[1:] + [action_line(ck, stop, size, size_note)])


# --- live position guard (market hours) ---

def _pos_tail(pos: dict, ltp: float, r: Optional[float], pace: Optional[float]) -> str:
    gain = (ltp / pos["avg_price"] - 1) * 100 if pos.get("avg_price") else None
    bits = [f"{pos['qty']} sh" if pos.get("qty") else "followed", f"{gain:+.1f}% from your {rs(pos['avg_price'])}"
            if gain is not None else None, f"{r:+.1f}R" if r is not None else None,
            f"volume {pace:.1f}× pace" if pace else None]
    return "<i>" + " · ".join(b for b in bits if b) + "</i>"


def live_stop_alert(pos: dict, ltp: float, r: Optional[float], pace: Optional[float], hard: bool,
                    check_time: str = "15:15") -> str:
    head = (f"🔻 <b>{escape(pos['symbol'])}</b> trading {rs(ltp)}, <b>under your stop</b> {rs(pos['stop'])} "
            f"({(ltp / pos['stop'] - 1) * 100:+.1f}%).")
    body = ("Hard stop: your rule says exit now." if hard else
            f"Unconfirmed until the close: the {_when(check_time)} check says whether it recovered. "
            "A hard stop says exit now.")
    return "\n".join([head, body, _pos_tail(pos, ltp, r, pace)])


def heavy_selling_alert(pos: dict, ltp: float, day_pct: float, pace: float, index_pct: Optional[float],
                        r: Optional[float]) -> str:
    above = (ltp / pos["stop"] - 1) * 100 if pos.get("stop") else None
    lines = [f"⚠️ <b>{escape(pos['symbol'])}</b> {day_pct:+.1f}% today at {rs(ltp)} on <b>{pace:.1f}× normal volume</b> "
             "for the time of day: heavy selling."
             + (f" Still above your stop {rs(pos['stop'])} ({above:.1f}% below)." if above is not None else "")]
    if index_pct is not None:
        lines.append(f"Nifty 500 {index_pct:+.1f}% today" + (": the stock is much weaker." if day_pct < index_pct - 2
                                                              else "."))
    lines.append(_pos_tail(pos, ltp, r, None))
    return "\n".join(lines)


def new_positions_alert(rows: list[dict]) -> str:
    """🆕 positions picked up from Kite during market hours."""
    lines = ["🆕 <b>Now following</b> (new in your Kite holdings)"]
    for p in rows:
        lines.append(f"<b>{escape(p['symbol'])}</b> {p['qty']} sh @ {rs(p['avg_price'])} · stop {rs(p['stop'])} "
                     f"({escape(p['stop_source'])}" + (f", risk {(1 - p['stop'] / p['avg_price']) * 100:.1f}%"
                                                      if p.get("avg_price") else "") + ")")
    lines.append("<i>Edit a stop on the dashboard; the live guard watches it from now on.</i>")
    return "\n".join(lines)


def gap_positions(broken: list[tuple[dict, float]], ok_count: int) -> str:
    lines = ["🌅 <b>Opening check · your positions</b>"]
    for pos, open_px in broken:
        lines.append(f"🔻 <b>{escape(pos['symbol'])}</b> opened {rs(open_px)}, <b>under your stop</b> {rs(pos['stop'])} "
                     f"({(open_px / pos['stop'] - 1) * 100:+.1f}%). A stop can't protect a gap: decide now.")
    if ok_count:
        lines.append(f"✅ {ok_count} other{'s' if ok_count > 1 else ''} opened above {'their stops' if ok_count > 1 else 'its stop'}.")
    return "\n".join(lines)


def close_positions(rows: list[tuple[dict, float, str]], fine: list[str], check_time: str = "15:15") -> str:
    """Close-check positions message: rows (pos, ltp, state under_stop / recovered / heavy)."""
    lines = [f"🕒 <b>Positions at {ampm(check_time)}</b> · act before 3:30 PM"]
    for pos, ltp, state in rows:
        sym, stop = escape(pos["symbol"]), rs(pos["stop"])
        if state == "under_stop":
            lines.append(f"🔻 <b>{sym}</b> {rs(ltp)}, still under the stop {stop}: <b>your rule says exit</b>.")
        elif state == "recovered":
            lines.append(f"✅ <b>{sym}</b> {rs(ltp)}, back above the stop {stop}.")
        else:
            lines.append(f"⚠️ <b>{sym}</b> {rs(ltp)}, sold off on heavy volume today; above the stop {stop}.")
    if fine:
        lines.append(f"<i>{escape(', '.join(fine))}: fine.</i>")
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
                zone_top: float, ctx: Optional[dict] = None, ck: Optional[dict] = None, job_time: str = "18:30") -> str:
    lines = [CLOSE_HEAD[verdict].format(sym=escape(p["symbol"])),
             f"{rs(ltp)}, {(ltp / p['pivot'] - 1) * 100:+.1f}% over the {rs(p['pivot'])} pivot "
             f"({escape(p['base_label'])}) · volume on pace {_pace(pace)} · "
             f"range {f'{rng:.2f}' if rng is not None else 'n/a'}"]
    if ck and ck["verdict"]:
        lines += checklist_lines(ck)
    if verdict == "holding":
        lines.append(f"Buy zone {rs(p['pivot'])} – {rs(zone_top)} · act before 3:30 PM. "
                     f"The {ampm(job_time)} scan confirms it.")
    elif verdict == "weak":
        lines.append(f"Volume or the close may not confirm: wait for the {ampm(job_time)} scan.")
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


def close_summary(items: list[dict], shown: set, check_time: str = "15:15") -> str:
    """One message for the close-check names not sent individually. items: {symbol, verdict, ltp, pivot, pace}."""
    groups = {"holding": "✅ Also holding", "weak": "🟡 Above pivot, weak", "extended": "⛔ Extended, don't chase"}
    lines = [f"🕒 <b>{ampm(check_time)} check</b>: {len(items)} names above their pivot"]
    for v, title in groups.items():
        names = [f"{'🅱️ ' if i.get('tier') == 'B' else ''}{escape(i['symbol'])} "
                 f"({(i['ltp'] / i['pivot'] - 1) * 100:+.1f}%, {_pace(i.get('pace'))})"
                 for i in items if i["verdict"] == v and i["symbol"] not in shown]
        if names:
            lines.append(f"{title}: " + ", ".join(names))
    return "\n".join(lines)


def confirm_note(flag: Optional[str], check_time: str = "15:15") -> Optional[str]:
    """Evening cross-reference to the close check."""
    t = ampm(check_time)
    return {"close_holding": f"✓ Flagged at {t} (holding into the close)",
            "close_weak": f"✓ Confirmed, though it looked weak at {t}",
            "close_extended": f"✓ Confirmed; it was extended at {t}"}.get(flag)


def not_confirmed_alert(symbol: str, pivot: float, close: Optional[float], check_time: str = "15:15") -> str:
    return (f"✗ <b>{escape(symbol)}</b> didn't confirm on the close: flagged as holding at {ampm(check_time)}, "
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


def results_short(r: dict) -> str:
    """'results Tue 28 Oct' for list lines."""
    from datetime import date
    d = date.fromisoformat(r["date"])
    return f"results {d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _plan_line(n: int, i: dict) -> str:
    tag = {"A": "🅰️ ", "B": "🅱️ "}.get(i.get("tier"), "")
    bits = [f"{n}. {tag}<b>{escape(i['symbol'])}</b> {escape(i.get('base_label') or '')}: close above "
            f"<b>{rs(i['pivot'])}</b> ({i['dist_to_pivot_pct']:.1f}% away) · buy zone to {rs(i['zone_top'])} · "
            f"stop {rs(i['stop'])}"]
    if i.get("size"):
        bits.append(f" · ~{i['size']['qty']} sh")
    if i.get("results"):
        bits.append(f"\n    ⚠️ {escape(results_short(i['results']))}: a results gap can jump the stop")
    fl = i.get("flags") or {}
    if fl.get("red"):
        bits.append(f"\n    🚩 {escape(fl['red'][0]['label'])}")
    if fl.get("buy"):
        bits.append(f"\n    🏦 {escape(fl['buy'][0]['label'])}")
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


def morning_plan(items: list[dict], plan_as_of: str, today: str, intraday_start: str = "09:20",
                 check_time: str = "15:15") -> str:
    lines = [f"☀️ <b>Today's plan</b> ({escape(today)}, from the {escape(plan_as_of)} scan)"]
    if not items:
        lines.append("No setups near a pivot today. Patience is a position.")
    lines += [_plan_line(n, i) for n, i in enumerate(items, 1)]
    lines.append(f"<i>Heads-up alerts from {ampm(intraday_start)} when a name trades above its pivot; the "
                 f"{ampm(check_time)} check says whether it's holding into the close.</i>")
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


GUARD_TEXT = {
    "stop": "🛑 <b>{sym}</b> closed {close}, below your stop {stop}. Your rule says exit.",
    "ma50": "📉 <b>{sym}</b> closed {close}, below its 50-day line ({ma50}). Trend break: consider exiting.",
    "profit": "💰 <b>{sym}</b> is up {gain:+.1f}% from your {avg}. Profit zone: consider selling part.",
    "extended": "🎈 <b>{sym}</b> closed {ext:.0f}% above its 50-day line: extended. Consider tightening the stop.",
    "failed": "⚠️ <b>{sym}</b> is back under the {pivot} pivot within {days} sessions of buying: failed breakout. "
              "Consider exiting.",
}


def guard_alert(rule: str, pos: dict, ev: dict, cfg) -> str:
    ext = (ev["close"] / ev["ma50"] - 1) * 100 if ev.get("ma50") else 0
    return GUARD_TEXT[rule].format(sym=escape(pos["symbol"]), close=rs(ev["close"]), stop=rs(pos["stop"]),
                                   ma50=rs(ev.get("ma50")), gain=ev["gain_pct"], avg=rs(pos["avg_price"]), ext=ext,
                                   pivot=rs(pos.get("pivot")), days=ev["days"]) + \
        f"\n<i>{pos['qty']} sh · {ev['gain_pct']:+.1f}%" + (f" · {ev['r']:+.1f}R" if ev.get("r") is not None else "") + \
        "</i>"


FILING_ICONS = {"results": "📊", "order": "📦", "deal": "🤝", "rating": "🏷️", "management": "👔", "legal": "⚖️",
                "exchange": "❓"}


def filing_alert(f: dict, why: str) -> str:
    from backend.ingest.filings import LABELS
    d = datetime.fromisoformat(f["dt"])
    text = f["text"] if len(f["text"]) <= 400 else f["text"][:397] + "…"
    lines = [f"{FILING_ICONS.get(f['kind'], '📰')} <b>{escape(f['symbol'])}</b> · {LABELS[f['kind']]} "
             f"<i>({escape(why)})</i>", escape(text),
             f"<i>{escape(f['category'])} · {d.day} {d:%b}, {ampm(d)}</i>"]
    if f.get("url", "").startswith("https://"):
        lines[-1] += f' · <a href="{escape(f["url"], quote=True)}">filing</a>'
    return "\n".join(lines)


def deal_alert(symbol: str, icon: str, item: dict, why: str) -> str:
    tail = " Holds it at tier B: no A alerts until it clears." if icon == "🚩" else ""
    return (f"{icon} <b>{escape(symbol)}</b> · {escape(item['label'])} <i>({escape(why)})</i>\n"
            f"{escape(item['text'])}.{tail}")


def guard_results(pos: dict, ev: dict, label: str) -> str:
    return (f"📅 <b>{escape(pos['symbol'])}</b>: {escape(label)}. A results gap can jump past your stop "
            f"{rs(pos['stop'])}: decide before the close whether to hold, trim or sell."
            f"\n<i>{pos['qty']} sh · {ev['gain_pct']:+.1f}%" + (f" · {ev['r']:+.1f}R" if ev.get("r") is not None else "")
            + "</i>")


def guard_summary(rows: list, as_of: str) -> str:
    lines = [f"📒 <b>Your positions</b> (EOD {escape(as_of)})"]
    for pos, ev in rows:
        warn = " ⚠️" if any(ev["fire"].values()) else ""
        lines.append(f"<b>{escape(pos['symbol'])}</b> {pos['qty']} sh · {rs(ev['close'])} · {ev['gain_pct']:+.1f}%"
                     + (f" · {ev['r']:+.1f}R" if ev.get("r") is not None else "")
                     + f" · day {ev['days']} · stop {rs(pos['stop'])}{warn}"
                     + (f" · 📅 {escape(results_short(ev['results']))}" if ev.get("results") else ""))
    return "\n".join(lines)


# --- Trend Breakout evening scan (signals only: never orders) -------------------------------------------

FILTER_ICON = {"ON": "✅", "FULL": "✅", "HALF": "🟡", "OFF": "⛔"}


def filter_line(fs: dict) -> str:
    """"A ⛔ OFF · E 🟡 HALF (5 slots)", with "(changed today)" when it flipped at today's close."""
    out = []
    for k, f in fs.items():
        top = f" · top {f['top']}" if f.get("top") else ""
        bit = f"<b>{k}</b>{top} {FILTER_ICON.get(f['state'], '')} {f['state']}"
        if f["state"] == "HALF":
            bit += f" ({f['slots']} slots)"
        if f.get("changed"):
            bit += f" (was {f['prev']})"
        out.append(bit)
    return " · ".join(out)


def _rup(v: float) -> str:
    return f"₹{v:,.0f}" if v >= 100 else rs(v)


def breakout_lines(res: dict, limit: int = 10) -> list[str]:
    """The scan as report lines: the market filter, the buys at the next open, the holdings HOLD / SELL at open."""
    lines = ["📈 <b>Trend Breakout</b> · signals only, no orders"]
    fs = res.get("filters") or {}
    if fs:
        lines.append("Filter: " + filter_line(fs))
    if res.get("nifty_sma200"):
        lines.append(f"NIFTY 50 {res['nifty']:,.0f} · 50-day {res.get('nifty_sma50') or 0:,.0f} · "
                     f"200-day {res['nifty_sma200']:,.0f}")
    buys = res.get("buys") or []
    if buys:
        lines.append(f"Breakouts today, ranked ({len(buys)}):")
        for n, b in enumerate(buys[:limit], 1):
            tag = ("A+E" if b.get("for") == ["A", "E"] else f"{'+'.join(b['for'])} only") if b.get("for") else ""
            lines.append(f"{n} <b>{escape(b['symbol'])}</b> {_rup(b['close'])} · stop {_rup(b['stop'])} "
                         f"({b['stop_pct']:.1f}%) · {b['qty']} sh {_rup(b['value'])} · risk {_rup(b['risk'])}"
                         + (f" · <i>{tag}</i>" if tag else ""))
        if len(buys) > limit:
            lines.append(f"+{len(buys) - limit} more on the Trend Breakout tab")
        if fs:
            lines.append(" · ".join(f"{k}: " + ("buy at the next open" if f["state"] in ("ON", "FULL")
                                                 else f"buy if a slot is free (≤ {f['slots']} open)" if f["state"] == "HALF"
                                                 else "not bought (filter off)") for k, f in fs.items()))
    else:
        lines.append("No breakouts today.")
    held = res.get("holdings") or []
    sells = [h for h in held if h["action"] != "HOLD"]
    if sells:
        lines += [f"🔴 <b>{escape(h['symbol'])}</b> SELL at open · {escape(h['why'])}" for h in sells]
    if held:
        keep = [h for h in held if h["action"] == "HOLD"]
        if keep:
            lines.append("🟢 HOLD: " + " · ".join(escape(h["symbol"]) for h in keep))
    elif not res.get("holdings_read"):
        lines.append("<i>Holdings not checked (log in to Kite).</i>")
    return lines


def breakout_scan(res: dict) -> str:
    """The scan as its own message (every-alert style)."""
    day = datetime.fromisoformat(res["as_of"])
    return "\n".join([f"{breakout_lines(res)[0]} · {day.day} {day:%b}"] + breakout_lines(res, 15)[1:])


# --- 📝 paper trading (simulated: never orders) ---------------------------------------------------------

PAPER = "📝 <b>PAPER TRADING</b>"


def _signed(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}₹{abs(v):,.0f}"


def _paper_account(key: str, r: dict) -> list[str]:
    a = r["account"]
    lines = [f"{'🔵' if key == 'A' else '🟣'} <b>{escape(r['name'])}</b> · {_rup(a['value'])} · {a['positions']} open",
             f"before costs {_signed(a['gross'])} · costs ₹{a['costs']:,.0f} · after <b>{_signed(a['net'])} "
             f"({a['return']:+.1f}%)</b>"]
    for f in r["fills"]:
        if f["action"] == "BUY":
            lines.append(f"Today: 🟢 BUY <b>{escape(f['symbol'])}</b> {f['qty']} @ {_rup(f['price'])} · stop {_rup(f['stop'])}")
        else:
            icon, how = ("🛑", "STOP") if f["why"] == "stop" else ("📉", "SOLD")
            lines.append(f"Today: {icon} {how} <b>{escape(f['symbol'])}</b> {f['qty']} @ {_rup(f['price'])} · "
                         f"{f['days']} days · gross {_signed(f['gross'])} · costs ₹{f['costs']:,.0f} · "
                         f"net <b>{_signed(f['net'])}</b>")
    orders = r.get("orders") or []
    sells = [o for o in orders if o["action"] == "SELL"]
    buys = [o for o in orders if o["action"] == "BUY"]
    if sells:
        lines.append("Tomorrow's open: " + " · ".join(f"SELL <b>{escape(o['symbol'])}</b> {o.get('qty', '')} ({escape(o['why'])})"
                                                     for o in sells))
    free = max(0, r.get("cap", 10) - a["positions"] + len(sells))
    for o in buys[:min(free, 5)]:
        lines.append(f"BUY <b>{escape(o['symbol'])}</b> ~{o.get('qty_est', '?')} · stop {_rup(o['stop'])} · "
                     f"risk {_rup(o.get('risk_est') or 0)}")
    if buys and free == 0:
        lines.append("No buys: no free slot")
    elif not buys and r.get("allowed") is False:
        lines.append("No buys: filter off")
    return lines


def paper_lines(rep: dict) -> list[str]:
    """Both paper accounts (A and E) side by side: the filter, today's fills, tomorrow's orders, before / after costs.
    Every block is labelled PAPER TRADING."""
    d = datetime.fromisoformat(rep["as_of"])
    lines = [f"{PAPER} · {d.day} {d:%b} · <i>simulated, no real orders</i>"]
    if rep.get("filters"):
        lines.append("Filter: " + filter_line(rep["filters"]))
    for key, r in rep["accounts"].items():
        lines += [""] + _paper_account(key, r)
    if rep.get("nifty") is not None:
        lines += ["", f"NIFTY 50 since the start {rep['nifty']:+.1f}%"]
    return lines


def paper_report(rep: dict) -> str:
    return "\n".join(paper_lines(rep))


# --- 💰 real trading (orders to Tradetron) ---------------------------------------------------------------

REAL = "💰 <b>REAL</b>"


def real_plan(res: dict, variant: str) -> str:
    """The evening: tomorrow's orders (sent at the morning send time)."""
    lines = [f"{REAL} · orders for the next open · {escape(tb_name(variant, res.get('top')))}"]
    orders = res.get("orders") or []
    for o in orders:
        if o["action"] == "EXIT":
            lines.append(f"🔴 EXIT <b>{escape(o['symbol'])}</b> {o['qty']} · {escape(o['why'])}")
        else:
            lines.append(f"🟢 BUY <b>{escape(o['symbol'])}</b> {o['qty']} · ~{_rup(o['qty'] * o['close'])} · "
                         f"stop {_rup(o['stop'])} · risk {_rup(o['risk'])}")
    if not orders:
        lines.append("No orders for tomorrow.")
    for o in res.get("missed") or []:
        lines.append(f"⚠️ Not sent today: {o['action']} <b>{escape(o['symbol'])}</b>")
    return "\n".join(lines)


def real_sent(res: dict, at: str) -> str:
    lines = [f"{REAL} · ORDERS SENT · {at}"]
    for o in res["sent"]:
        lines.append(f"{'🔴 EXIT' if o['action'] == 'EXIT' else '🟢 BUY'} <b>{escape(o['symbol'])}</b>"
                     + (f" {o['qty']} · stop {_rup(o['stop'])}" if o["action"] == "BUY" else f" {o['qty']}")
                     + f" · ~{_rup(o['price'])}")
    for o in res["failed"]:
        lines.append(f"⚠️ FAILED {o['action']} <b>{escape(o['symbol'])}</b>: {escape(o['error'])} (not retried)")
    lines.append(f"Tradetron: {len(res['sent'])} of {len(res['sent']) + len(res['failed'])} accepted")
    return "\n".join(lines)


def real_stop(h: dict, at: str) -> str:
    if h.get("error"):
        return (f"{REAL} · ⚠️ STOP HIT, EXIT FAILED · {at}\n<b>{escape(h['symbol'])}</b> {h['qty']} at {_rup(h['price'])}: "
                f"{escape(h['error'])}. Exit it yourself.")
    return (f"{REAL} · STOP HIT · EXIT SENT · {at}\n🛑 <b>{escape(h['symbol'])}</b> {h['qty']} · traded {_rup(h['price'])} "
            f"≤ {escape(h['why'])} · net {_signed(h['net'])}")


def real_nologin(at: str) -> str:
    return (f"{REAL} · 🚨 STOPS NOT WATCHED · {ampm(at)}\nKite is not logged in and real positions are open. "
            "Log in to Kite now: the first check after the login exits any position whose stop was touched meanwhile.")


def tb_name(variant: str, top: Optional[int] = None) -> str:
    base = {"A": "A · 200-day filter", "E": "E · half filter"}.get(variant, variant)
    return f"{base}, top {top}" if top else base


def flow_signal(s: dict) -> str:
    """⚡ Intraday Flow signal (phase 1: signals only, no order)."""
    paper = s.get("mode") == "paper"
    head = "📝 <b>PAPER INTRADAY</b>" if paper else "⚡ <b>INTRADAY SIGNAL</b>"
    bits = [f"{head} · {escape(s.get('signal') or 'BUY')} <b>{escape(s['symbol'])}</b> at {_rup(s['entry'])}"
            + (f" · {s['qty']} sh · risk {_rup(s['risk_rs'])}" if s.get("qty") else "")]
    if s.get("sl"):
        bits.append(f"SL {_rup(s['sl'])} · target {_rup(s['target'])}")
    extra = [f"RVOL {s['rvol']}×" if s.get("rvol") else "", f"RSI {s['rsi']}" if s.get("rsi") else ""]
    if any(extra):
        bits.append(" · ".join(x for x in extra if x))
    bits.append("<i>paper trade (simulated), fills at the next minute's open</i>" if paper else "<i>signal only, no order placed</i>")
    return "\n".join(bits)


def flow_event(p: dict, ev: dict) -> str:
    side = "BUY" if p["side"] == "long" else "SELL"
    if "fill" in ev:
        return (f"📝 <b>PAPER INTRADAY</b> · filled {side} <b>{escape(p['symbol'])}</b> {ev['qty']} @ {_rup(ev['fill'])} · "
                f"SL {_rup(p['stop0'])}" + (f" · target {_rup(p['target'])}" if p.get("target") else ""))
    return (f"📝 <b>PAPER INTRADAY</b> · exit <b>{escape(p['symbol'])}</b> {ev['qty']} @ {_rup(ev['price'])} · {escape(ev['why'])} · "
            f"gross {_signed(ev['gross'])} · costs ₹{ev['costs']:,.0f} · net <b>{_signed(ev['net'])}</b>")


def flow_loss(st: dict) -> str:
    return (f"📝 <b>PAPER INTRADAY</b> · 🛑 daily loss limit hit: {_signed(st['realised'])} (limit −₹{st['loss_limit']:,.0f}). "
            "No new entries today; open positions keep their exits.")


def flow_summary(st: dict) -> str:
    return (f"📝 <b>PAPER INTRADAY · day summary</b>\n{st['trades']} trades · {st['won']} won · net after costs "
            f"<b>{_signed(st['realised'])}</b> on ₹{st['capital']:,.0f}")
