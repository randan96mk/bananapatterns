"""Telegram message text (HTML parse mode). Every value is escaped."""

from datetime import datetime
from html import escape
from typing import Optional

from backend.timefmt import ampm, ampm_date

ICONS = {"breakout": "🚀", "breakout_low_volume": "⚠️", "below_pivot": "🔻", "played_out": "🏁",
         "forming": "🔵", "left": "▫️", "climbing": "📈", "near_pivot": "🎯", "intraday": "⚡",
         "system": "⚙️", "digest": "📋"}
SCREEN_NAMES = {"vcp": "VCP", "flag": "Bull flag", "bases": "Bases"}
SCREEN_PREF = {"vcp": 0, "flag": 1, "bases": 2}   # a name on several screens: the most specific one
SCREEN_ORDER_SQL = "CASE {col} WHEN 'vcp' THEN 0 WHEN 'flag' THEN 1 ELSE 2 END"


def setup_stop(p: dict) -> Optional[float]:
    """The setup's own stop: the VCP stop, else the flag's (flag low -
    buffer), else None."""
    return (p.get("vcp") or {}).get("stop") or (p.get("flag") or {}).get("stop")


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
        v, flag, stop = p.get("vcp") or {}, p.get("flag"), setup_stop(p)
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
    lines = ["🆕 <b>Now following</b>"]
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
    """"E · top 300 🟡 HALF (5 slots)", with "(was FULL)" when it flipped at today's close."""
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
    """The scan as report lines: the market filter and the buys at the next open."""
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
            lines.append(f"{n} <b>{escape(b['symbol'])}</b> {_rup(b['close'])} · stop {_rup(b['stop'])} "
                         f"({b['stop_pct']:.1f}%) · {b['qty']} sh {_rup(b['value'])} · risk {_rup(b['risk'])}")
        if len(buys) > limit:
            lines.append(f"+{len(buys) - limit} more on the Trend Breakout tab")
        if fs:
            lines.append(" · ".join(f"{k}: " + ("buy at the next open" if f["state"] in ("ON", "FULL")
                                                 else f"buy if a slot is free (≤ {f['slots']} open)" if f["state"] == "HALF"
                                                 else "not bought (filter off)") for k, f in fs.items()))
    else:
        lines.append("No breakouts today.")
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
    lines = [f"🟣 <b>{escape(r['name'])}</b> · {_rup(a['value'])} · {a['positions']} open",
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
    """The paper account (E+): the filter, today's fills, tomorrow's orders, before / after costs. Every block is
    labelled PAPER TRADING."""
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


def _wday(d: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(d).strftime("%a") if d else ""
    except ValueError:
        return d or ""


def real_preview(orders: list, variant: str, send_at: str, on: bool, killed: bool, m: Optional[dict] = None) -> str:
    """The morning reminder (and the reply to skip / unskip / hold): the orders that go out at send_at, the ones
    skipped today and why, the account facts, and the one-line way to skip or hold before the send. `m` is the
    morning view (real.morning); without it only the orders are listed."""
    skips = {o["symbol"]: o["skip"] for o in (m or {}).get("orders") or [] if o.get("skip")}
    results = {o["symbol"]: o for o in (m or {}).get("orders") or [] if o.get("results_date")}
    going = [o for o in orders if o["action"] == "EXIT" or o["symbol"] not in skips]
    n = len(going)
    state = "🔴 kill switch ON: nothing goes out" if killed else "OFF: nothing goes out" if not on else f"go out at {ampm(send_at)}"
    lines = [f"☀️ {REAL} · {n} order{'s' if n != 1 else ''} {state} · {escape(tb_name(variant, None))}"]
    for o in going:
        if o["action"] == "EXIT":
            lines.append(f"🔴 EXIT <b>{escape(o['symbol'])}</b> {o['qty']} · {escape(o.get('why') or '')}")
        else:
            r = results.get(o["symbol"])
            tag = f" · ⚠️ results {_wday(r['results_date'])}" if r else ""
            lines.append(f"🟢 BUY <b>{escape(o['symbol'])}</b> {o['qty']} · stop {_rup(o['stop'])} · ~{_rup(o['qty'] * o['close'])}{tag}")
    if not going:
        lines.append("Nothing goes out today.")
    if skips:
        why = lambda s, k: f"results {_wday(k.get('results_date'))}" if k.get("results_date") else (k.get("note") or f"by {k['by']}")
        lines.append("<i>skipped today: " + " · ".join(f"{escape(s)} ({escape(why(s, k))})" for s, k in skips.items()) + "</i>")
    if m and m.get("started"):
        facts = [f"cash {_rup(m['cash'])}" if m.get("cash") is not None else "", f"{m.get('free', 0)} free slot{'s' if m.get('free') != 1 else ''}",
                 f"filter {escape(m['filter'])}" if m.get("filter") else "", f"gap {m['gap_s']:g} s" if m.get("gap_s") else ""]
        lines.append("<i>" + " · ".join(x for x in facts if x) + "</i>")
    if on and not killed:
        lines.append(f"\nReply <b>skip SYMBOL</b> or <b>hold</b> before {ampm(send_at)} · or open the Real page. "
                     "The kill switch and the enable switch stay as they are.")
    return "\n".join(lines)


def real_sent(res: dict, at: str) -> str:
    lines = [f"{REAL} · ORDERS SENT · {at}"]
    for o in res["sent"]:
        lines.append(f"{'🔴 EXIT' if o['action'] == 'EXIT' else '🟢 BUY'} <b>{escape(o['symbol'])}</b>"
                     + (f" {o['qty']} · stop {_rup(o['stop'])}" if o["action"] == "BUY" else f" {o['qty']}")
                     + f" · ~{_rup(o['price'])}"
                     + (f" · accepted after {o['retries']} retr{'y' if o['retries'] == 1 else 'ies'} (Tradetron was busy)"
                        if o.get("retries") else ""))
    for o in res["failed"]:
        why = "still too many requests after 3 retries" if "429" in str(o["error"]) else "not retried"
        lines.append(f"⚠️ FAILED {o['action']} <b>{escape(o['symbol'])}</b>: {escape(o['error'])} ({why})")
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
    base = {"E": "E+ · half filter"}.get(variant, variant)
    if variant == "E" and top:
        return f"{base}, top {top}, late trail"
    return f"{base}, top {top}" if top else base


def flow_signal(s: dict) -> str:
    """⚡ Intraday Flow signal (phase 1: signals only, no order)."""
    paper = s.get("mode") == "paper"
    head = "📝 <b>PAPER INTRADAY</b>" if paper else "⚡ <b>INTRADAY SIGNAL</b>"
    bits = [f"{head} · {escape(s.get('signal') or 'BUY')} <b>{escape(s['symbol'])}</b> at {_rup(s['entry'])}"
            + (f" · {s['qty']} sh · risk {_rup(s['risk_rs'])}" if s.get("qty") else "")]
    if s.get("sl"):
        bits.append(f"SL {_rup(s['sl'])} · " + (f"target {_rup(s['target'])}" if s.get("target") else "no target (out at square-off)"))
    extra = [f"RVOL {s['rvol']}×" if s.get("rvol") else "", f"RSI {s['rsi']}" if s.get("rsi") else ""]
    if any(extra):
        bits.append(" · ".join(x for x in extra if x))
    bits.append("<i>paper trade (simulated), fills at the next minute's open</i>" if paper else "<i>signal only, no order placed · tracked: you'll get its fill, stop / target / exit as alerts</i>")
    return "\n".join(bits)


def _flow_head(kind: str = "paper") -> str:
    return "⚡ <b>SIGNAL TRACK</b> <i>(not traded)</i>" if kind == "signal" else "📝 <b>PAPER INTRADAY</b>"


def flow_event(p: dict, ev: dict) -> str:
    side = "BUY" if p["side"] == "long" else "SELL"
    head = _flow_head(p.get("kind", "paper"))
    if "cancel" in ev:
        return (f"{head} · {side} <b>{escape(p['symbol'])}</b> cancelled: {escape(ev['cancel'])} "
                f"(limit {_rup(ev['limit'])})")
    if "fill" in ev:
        return (f"{head} · filled {side} <b>{escape(p['symbol'])}</b> {ev['qty']} @ {_rup(ev['fill'])} · "
                f"SL {_rup(p['stop0'])}" + (f" · target {_rup(p['target'])}" if p.get("target") else ""))
    return (f"{head} · exit <b>{escape(p['symbol'])}</b> {ev['qty']} @ {_rup(ev['price'])} · {escape(ev['why'])} · "
            f"gross {_signed(ev['gross'])} · costs ₹{ev['costs']:,.0f} · net <b>{_signed(ev['net'])}</b>")


def flow_loss(st: dict, kind: str = "paper") -> str:
    return (f"{_flow_head(kind)} · 🛑 daily loss limit hit: {_signed(st['realised'])} (limit −₹{st['loss_limit']:,.0f}). "
            "No new entries today; open positions keep their exits.")


def flow_summary(st: dict, kind: str = "paper") -> str:
    return (f"{_flow_head(kind)} · <b>day summary</b>\n{st['trades']} trades · {st['won']} won · net after costs "
            f"<b>{_signed(st['realised'])}</b> on ₹{st['capital']:,.0f}")


def flow_switch(kind: str, st: dict) -> str:
    if kind == "started":
        return "▶ <b>INTRADAY STARTED</b>\nLive data and the flow resume from the next candle."
    return (f"⏹ <b>INTRADAY STOPPED</b>\n{st.get('squared', 0)} paper positions squared off · today net after costs "
            f"<b>{_signed(st.get('realised', 0))}</b>\nNo live data, signals or trades until ▶ Start.")


def trail_raised(where: str, sym: str, stop: float, best: float, entry: float) -> str:
    """E+'s late trail moved a stop up (after the close; it applies from the next session)."""
    return (f"{where} · <b>E+ · {escape(sym)}</b> trail stop raised to <b>₹{stop:,.2f}</b> (best close ₹{best:,.2f}, "
            f"+{(best / entry - 1) * 100:.0f}% over the entry {_rup(entry)}) · from the next session"
            + ("\nWatched by the app like any stop: an EXIT goes to Tradetron automatically if it is hit (while real "
               "trading is on). A stop sent to Tradetron with the BUY is not moved." if where == REAL else ""))


# --- 📐 stock futures (the book: paper · long / real · long / paper · short) -------------------------------------

FUT = "📐 <b>FUTURES</b>"


def _fut_tag(tag: str) -> str:
    return f"<i>{escape(tag)}</i>"


def _lots(o: dict) -> str:
    return f"{o.get('lots', 1)} lot{'s' if (o.get('lots') or 1) != 1 else ''} ({(o.get('lots') or 1) * (o.get('lot_size') or 0):,})"


def futures_plan(res: dict, tags: dict) -> str:
    """The evening: the next open's futures orders per side, the waiting signals with their reasons."""
    lines = [f"{FUT} · plan for the next open"]
    plan = res.get("plan") or {}
    for side in ("long", "short"):
        for o in plan.get(side, []):
            tag = " / ".join(tags.get(side) or []) or "off"
            if o["action"] == "EXIT":
                lines.append(f"🔴 EXIT <b>{escape(o['symbol'])}</b> {_lots(o)} · {escape(o.get('why') or '')} · {_fut_tag(tag)}")
            else:
                lines.append(f"{'🟢 BUY' if o['action'] == 'BUY' else '🔻 SELL'} <b>{escape(o['symbol'])}</b> {_lots(o)} · ~{_rup(o['entry'])} · "
                             f"stop {_rup(o['stop'])} ({o.get('stop_pct', 0):.1f}%) · margin {_rup(o.get('margin_est') or 0)} · {_fut_tag(tag)}")
    if not any(plan.get(s) for s in ("long", "short")):
        lines.append("No futures orders for tomorrow.")
    waiting = [w for s in ("long", "short") for w in (res.get("waiting") or {}).get(s, [])]
    if waiting:
        lines.append("<i>waiting: " + " · ".join(f"{escape(w['symbol'])} {w['side']} ({escape(' · '.join(w['reasons']))})" for w in waiting[:6])
                     + (" · …" if len(waiting) > 6 else "") + "</i>")
    for o in res.get("missed") or []:
        lines.append(f"⚠️ Not sent today: {o['action']} <b>{escape(o['symbol'])}</b>")
    if res.get("roll_day"):
        lines.append(f"<i>next roll day {_wday(res['roll_day'])} {res['roll_day'][8:]}/{res['roll_day'][5:7]} (T-2)</i>")
    return "\n".join(lines)


def futures_preview(m: dict, send_at: str) -> str:
    """☀️ The 9:00 dry run: lots and margin per order, the skips, the one-line way to skip or hold (`fut skip SYM`,
    `fut hold`) before the send. Each order carries its tags (paper · long, real · long, paper · short)."""
    g = m.get("guard") or {}
    orders = [o for o in m.get("orders") or [] if not o.get("skip") and o.get("status") != "skipped"]
    n = len(orders)
    real_on = bool(g.get("real_sides")) and g.get("enabled") and not g.get("killed")
    state = "🔴 kill switch ON: nothing goes out" if g.get("killed") else "OFF: paper only" if not g.get("enabled") else \
        (f"real sides go out at {ampm(send_at)} · paper fills at the open" if real_on else "paper only · fills at the open")
    lines = [f"☀️ {FUT} · {n} order{'s' if n != 1 else ''} · {state}"]
    for o in orders:
        tags = " / ".join(f"{b} · {o['side']}" for b in o.get("books") or []) or "off"
        if o["action"] == "EXIT":
            lines.append(f"🔴 EXIT <b>{escape(o['symbol'])}</b> {_lots(o)} · {escape(o.get('why') or '')} · {_fut_tag(tags)}")
        else:
            tag = f" · ⚠️ results {_wday(o['results_date'])}" if o.get("results_date") else ""
            lines.append(f"{'🟢 BUY' if o['action'] == 'BUY' else '🔻 SELL'} <b>{escape(o['symbol'])}</b> {_lots(o)} · stop {_rup(o['stop'])} · "
                         f"margin {_rup(o.get('margin') or 0)} · ≈ {_rup(o.get('value') or 0)}{tag} · {_fut_tag(tags)}")
    if not orders:
        lines.append("Nothing goes out today.")
    skips = {o["symbol"]: o["skip"] for o in m.get("orders") or [] if o.get("skip")}
    if skips:
        why = lambda k: f"results {_wday(k.get('results_date'))}" if k.get("results_date") else (k.get("note") or f"by {k['by']}")
        lines.append("<i>skipped today: " + " · ".join(f"{escape(s)} ({escape(why(k))})" for s, k in skips.items()) + "</i>")
    if m.get("started"):
        facts = [f"cash {_rup(m['cash'])}" if m.get("cash") is not None else "", f"margin used {_rup(m['margin_used'])}" if m.get("margin_used") is not None else "",
                 f"{m.get('free', 0)} free slot{'s' if m.get('free') != 1 else ''}", f"filter {escape(m['filter'])}" if m.get("filter") else "",
                 f"margin needed {_rup((m.get('going_out') or {}).get('margin') or 0)}"]
        lines.append("<i>" + " · ".join(x for x in facts if x) + "</i>")
    lines.append(f"\nReply <b>fut skip SYMBOL</b> or <b>fut hold</b> before {ampm(send_at)} · or open the Futures page. "
                 "The kill switch and the enable switch stay as they are.")
    return "\n".join(lines)


def futures_sent(res: dict, at: str) -> str:
    lines = [f"{FUT} · ORDERS SENT · {at}"]
    for o in res.get("sent") or []:
        lines.append(f"{'🔴 EXIT' if o['action'] == 'EXIT' else '🟢 BUY' if o['action'] == 'BUY' else '🔻 SELL'} <b>{escape(o['symbol'])}</b> {_lots(o)}"
                     + (f" · stop {_rup(o['stop'])}" if o.get("stop") and o["action"] != "EXIT" else "") + f" · ~{_rup(o['price'])} · {_fut_tag(o.get('tag') or 'real')}")
    for o in res.get("failed") or []:
        lines.append(f"⚠️ FAILED {o['action']} <b>{escape(o['symbol'])}</b>: {escape(str(o['error']))} (not retried)")
    lines.append(f"accepted {len(res.get('sent') or [])} of {len(res.get('sent') or []) + len(res.get('failed') or [])}")
    return "\n".join(lines)


def futures_stop(h: dict, at: str) -> str:
    """A stop hit on the futures price (paper closes, real sends EXIT)."""
    tag = _fut_tag(h.get("tag") or "")
    if h.get("error"):
        return (f"{FUT} · ⚠️ STOP HIT, EXIT FAILED · {at}\n<b>{escape(h['symbol'])}</b> {h.get('lots', 1)} lot at {_rup(h['price'])}: "
                f"{escape(h['error'])}. Exit it yourself. {tag}")
    return (f"{FUT} · STOP HIT · {at}\n🛑 <b>{escape(h['symbol'])}</b> {h['side']} {h.get('lots', 1)} lot · futures {_rup(h['price'])} · "
            f"{escape(h['why'])} · net {_signed(h.get('net') or 0)} · {tag}")


def futures_roll_notice(items: list, roll_day: str, when: str) -> str:
    """T-3: the roll notice (what rolls or exits at T-2); the roll day: the lines of what was done."""
    head = f"{FUT} · ↻ roll {'notice' if when == 'notice' else 'done'} · {_wday(roll_day)} {roll_day[8:]}/{roll_day[5:7]} (T-2)"
    lines = [head]
    for it in items:
        tag = _fut_tag(it.get("tag") or TAG_OF.get(it.get("account"), ""))
        if when == "notice":
            what = f"EXIT {escape(it.get('contract') or '')} + {'BUY' if it['side'] == 'long' else 'SELL'} {escape(it.get('next') or '?')}" if it["action"] == "roll" \
                else f"EXIT {escape(it.get('contract') or '')} ({escape(it.get('why') or '')})"
            lines.append(f"<b>{escape(it['symbol'])}</b> {it['side']} · {what} · est. cost {_rup(it.get('est_cost') or 0)} · {tag}")
        else:
            lines.append(f"<b>{escape(it['symbol'])}</b> {it['side']} · {escape(it['from'])} → {escape(it['to'])} · {_rup(it['px'])} → {_rup(it['px_next'])} · "
                         f"basis {it.get('basis_pct') if it.get('basis_pct') is not None else '—'}% · cost {_rup(it['cost'])} · {tag}")
    if not items:
        lines.append("Nothing to roll.")
    lines.append("<i>never held into expiry · a lot whose rule fired is exited instead of rolled</i>")
    return "\n".join(lines)


TAG_OF = {"paper_long": "paper · long", "real_long": "real · long", "paper_short": "paper · short", "real_short": "real · short"}


def futures_daily_loss(tag: str, used: float, limit: float) -> str:
    return (f"{FUT} · 🛑 daily loss limit reached · {_fut_tag(tag)}\n{_signed(-used)} today (limit −{_rup(limit)}). "
            "No new lots today; stops keep running.")


def futures_margin(tag: str, pct: float, used: float, warn_at: float = 70.0, block_at: float = 80.0) -> str:
    return (f"{FUT} · ⚠️ margin {pct:.0f}% of capital · {_fut_tag(tag)}\nmargin used {_rup(used)}. "
            f"Warned at {warn_at:.0f}%; no new lots at {block_at:.0f}%.")


def futures_held_warnings(w: dict) -> Optional[str]:
    """Daily: an MWPL ban on a held name, results within 3 sessions, a lot size change."""
    lines = []
    if w.get("ban"):
        lines.append("⛔ in the MWPL ban list: " + ", ".join(f"<b>{escape(s)}</b>" for s in w["ban"]) + " · no new positions; exits still allowed")
    for r in w.get("results") or []:
        lines.append(f"📊 results {_wday(r['date'])} (in {r['sessions']} session{'s' if r['sessions'] != 1 else ''}): <b>{escape(r['symbol'])}</b> · gap risk")
    for c in w.get("lot_change") or []:
        lines.append(f"📦 lot size change <b>{escape(c['symbol'])}</b>: {c['from']:,} → {c['to']:,} · the open lot keeps {c['from']:,}; the next entry uses {c['to']:,}")
    if not lines:
        return None
    return f"{FUT} · held names · {_wday(w.get('day'))}\n" + "\n".join(lines)


def futures_short_signals(sigs: list, plan: list, waiting: list) -> Optional[str]:
    """The short side's paper signals of the evening, tagged paper · short."""
    if not sigs:
        return None
    taken = {o["symbol"] for o in plan if o["action"] == "SELL"}
    why = {w["symbol"]: " · ".join(w["reasons"]) for w in waiting}
    lines = [f"{FUT} · E− signals · {_fut_tag('paper · short')}"]
    for s in sigs[:8]:
        state = "SELL at the next open (paper)" if s["symbol"] in taken else f"waits: {escape(why.get(s['symbol'], 'not eligible'))}"
        lines.append(f"🔻 <b>{escape(s['symbol'])}</b> · swing high {_rup(s['hint'])} · {state}")
    lines.append("<i>shorts stay paper until Backtest F's short verdict passes</i>")
    return "\n".join(lines)


# --- 🧩 NIFTY options (the book: paper only; a credit spread on the opening-range bias) --------------------------------

OPT = "Options"


def inr(v: Optional[float], d: int = 0, sign: bool = False) -> str:
    """Rupees with the Indian grouping (₹3,06,800 · ₹52,000 · ₹1,23,45,678.50); `sign` prefixes +/− (a minus is U+2212)."""
    if v is None:
        return "—"
    neg = v < 0
    whole, _, frac = f"{abs(v):.{d}f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    num = ",".join(groups + [tail]) if whole and len(whole) > 3 else whole
    out = f"₹{num}" + (f".{frac}" if frac else "")
    return ("−" if neg else "+" if sign else "") + out


def _opt_head(*bits: str) -> str:
    return "<b>" + " · ".join([OPT] + [escape(str(b)) for b in bits if b]) + "</b>"


def _strike(leg: dict) -> str:
    return f"{leg['strike']:,.0f} {escape(leg['type'])}"


def _held(opened_at: Optional[str], closed_at: Optional[str]) -> str:
    """'2h 51m' between two ISO stamps ('' when unknown)."""
    if not opened_at or not closed_at:
        return ""
    try:
        m = int((datetime.fromisoformat(closed_at) - datetime.fromisoformat(opened_at)).total_seconds() // 60)
    except ValueError:
        return ""
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def _day_label(iso: Optional[str]) -> str:
    """'Fri 14 Nov' from '2026-11-14'."""
    if not iso:
        return ""
    d = datetime.fromisoformat(iso[:10])
    return f"{d:%a} {d.day} {d:%b}"


def options_bias(o: dict, tag: str = "paper") -> str:
    """🧭 The bias found and the spread sold: the price line (the range, VWAP, the previous day's level), the two legs with
    the lots and the credit, stop / target / worst case, margin / sessions / VIX dim, the chain line. `o` is the open
    spread the book recorded (plus range / prev / vix / close)."""
    bias, s, h = o["bias"], o["sold"], o["hedge"]
    bull = bias == "BULLISH"
    rng, prev = o.get("range") or {}, o.get("prev") or {}
    px = o.get("close") or o.get("spot_at_entry") or o.get("spot")
    line = [f"NIFTY {px:,.0f}" if px else "NIFTY", f"{'above' if bull else 'below'} the 9:15–9:30 range"
            + (f" ({rng['low']:,.0f}–{rng['high']:,.0f})" if rng else "")]
    if o.get("vwap_at_entry") or o.get("vwap"):
        line.append(f"and VWAP {(o.get('vwap_at_entry') or o.get('vwap')):,.0f}")
    price = " ".join(line)
    lvl = prev.get("high") if bull else prev.get("low")
    if lvl:
        price += f" · prev {'high' if bull else 'low'} taken"
    lots = o["lots"]
    legs = (f"SELL <b>{_strike(s)}</b> @ {s['entry']:.2f} · BUY {_strike(h)} @ {h['entry']:.2f} · {lots} lot{'s' if lots != 1 else ''} · "
            f"credit {inr(o['credit_rs'])}")
    stop = (f"stop {o['stop_premium']:.2f} (+{o['stop_pct']:g}%) or a 5-min close {'under' if bull else 'over'} VWAP · "
            f"target {o['target_value']:.2f} ({o.get('target_pct', 60):g}%) · worst case {inr(-o['worst_case'], sign=True)}")
    dim = [f"margin ~{inr(o['margin_est'])}"]
    if o.get("sessions") is not None:
        dim.append("expiry day" if o["sessions"] == 0 else f"{o['sessions']} session{'s' if o['sessions'] != 1 else ''} to expiry")
    if o.get("vix"):
        dim.append(f"VIX {o['vix']:.1f}")
    out = [f"🧭 {_opt_head(tag, f'bias {bias}', o.get('opened_t'))}", escape(price), legs, escape(stop), f"<i>{escape(' · '.join(dim))}</i>"]
    if o.get("chain_line"):
        out.append(f"<i>chain: {escape(o['chain_line'])}</i>")
    if (o.get("n") or 1) > 1:
        out.insert(1, "<i>re-entry: the bias flipped</i>")
    return "\n".join(out)


EXIT_ICON = {"target": "🎯", "premium stop": "🛑", "vwap stop": "🛑", "breakeven stop": "🛑", "time exit": "⏱", "hard exit": "⏱",
             "kill": "🔴", "closed by you": "✋"}


def options_exit(kind: str, t: dict, tag: str = "paper") -> str:
    """🎯 / 🛑 / ⏱ the spread closed: the strikes, the exit value, the P&L, the time held, the breakeven stop when it was
    armed, 'at the last known mid' when no quote was there. `t` is the trade row."""
    from backend.options.book import EXIT_LABEL
    s, h = t["sold"], t["hedge"]
    label = EXIT_LABEL.get(kind, kind)
    bits = [f"{s['strike']:,.0f} / {h['strike']:,.0f} {escape(s['type'])} closed at {t['cost']:.2f}",
            f"<b>{inr(t['net'], sign=True)}</b>"]
    held = _held(t.get("opened_at"), t.get("closed_at"))
    if held:
        bits.append(f"held {held}")
    if t.get("be_active") and kind != "breakeven stop" and t.get("be_t"):
        bits.append(f"breakeven stop was armed at {t['be_t']}")
    if kind == "breakeven stop" and t.get("be_t"):
        bits.append(f"armed at {t['be_t']}")
    if t.get("est"):
        bits.append("at the last known mid (no quote)")
    if (t.get("lots") or 1) != 1:
        bits.insert(1, f"{t['lots']} lots")
    return f"{EXIT_ICON.get(kind, '🏁')} {_opt_head(tag, label)} · " + " · ".join(bits)


def options_no_trade(d: dict) -> str:
    """🧭 11:00: no break of the range; where VWAP sat; the next look (the next trading day at window_start)."""
    rng = d.get("range") or {}
    inside = f"{rng['low']:,.0f}–{rng['high']:,.0f}" if rng else "no range"
    if d.get("vwap_where"):
        inside += f" · VWAP {d['vwap_where']}"
    nxt = f" · next look {_day_label(d['next'])[:3]} {ampm(d.get('window_start') or '09:45')}" if d.get("next") else ""
    return f"🧭 {_opt_head('no trade today')} · no break of the 9:15–9:30 range by {ampm(d.get('window_end') or '11:00')} ({escape(inside)}){escape(nxt)}"


def options_summary(row: dict, book: dict, tag: str = "paper") -> str:
    """🌙 15:35: the day (trades, P&L), the book (value, return since the start), the days traded, the win rate."""
    n = row.get("trades") or 0
    bits = [f"{n} trade{'s' if n != 1 else ''}" if n else f"no trade ({escape(str(row.get('why') or row.get('result') or 'no bias'))})"]
    if n:
        bits.append(f"<b>{inr(row.get('pnl') or 0.0, sign=True)}</b>")
        if row.get("result"):
            bits.append(escape(row["result"]))
    since = _day_label(book.get("started"))
    bits.append(f"book {inr(book['equity'])} ({book['return']:+.1f}%" + (f" since {since[4:]}" if since else "") + ")")
    if book.get("days"):
        bits.append(f"{book.get('traded', 0)} traded of {book['days']} day{'s' if book['days'] != 1 else ''}")
    if book.get("traded"):
        bits.append(f"won {int(book.get('won', 0) / book['traded'] * 100 + 0.5)}%")
    return f"🌙 {_opt_head(tag, _day_label(row.get('day')))} · " + " · ".join(bits)


def options_chain_disagrees(d: dict, tag: str = "paper") -> str:
    return f"🧭 {_opt_head(tag, 'bias found · chain disagrees')} · {escape(d.get('bias') or '')} · no trade · <i>{escape(d.get('line') or 'score 0/3')}</i>"


def options_daily_loss(d: dict, tag: str = "paper") -> str:
    return (f"🛑 {_opt_head(tag, 'daily loss limit reached')} · {inr(-(d.get('used') or 0), sign=True)} today (limit −{inr(d.get('limit') or 0)}) · "
            "no new entries today; the open spread keeps its stops")


def options_skipped(kind: str, d: dict, tag: str = "paper") -> str:
    """event_day / skipped: a bias was found but a guard said no entry."""
    why = " · ".join(d.get("reasons") or [])
    head = "event day · no entry" if kind == "event_day" else "no entry"
    return f"📅 {_opt_head(tag, head)} · {escape(d.get('bias') or '')} found · {escape(why)}"


def options_vix_half(d: dict, tag: str = "paper") -> str:
    return (f"🌪 {_opt_head(tag, 'half size')} · VIX {d.get('vix') or 0:.1f} over {d.get('half_at') or 20:g} · "
            f"{d.get('lots', 1)} lot{'s' if d.get('lots', 1) != 1 else ''} today")


def options_no_size(kind: str, d: dict, tag: str = "paper") -> str:
    """no_trade_size (0 lots after the halvings) / no_trade_strikes (no sold strike with enough premium)."""
    why = " · ".join(d.get("notes") or []) if kind == "no_trade_size" else (d.get("why") or "no strike")
    return f"🧭 {_opt_head(tag, 'no trade')} · {escape(d.get('bias') or '')} found · {escape(why)}"


def options_no_data(d: dict, tag: str = "paper") -> str:
    why = d.get("why") or "Kite login missing"
    tail = " · log in from the dashboard; the day is recorded as no data" if "login" in why else ""
    return f"⚠️ {_opt_head(tag, 'no data')} · {escape(why)}{tail}"


def options_status(s: dict) -> str:
    """The `opt` command's reply: today's status, the open spread with its P&L, today's realised, the book."""
    t, o = s.get("today") or {}, s.get("open")
    sw = s.get("switches") or {}
    state = "🔴 kill switch ON" if sw.get("killed") else "OFF" if not sw.get("enabled") else "paper" if sw.get("paper") else "idle"
    lines = [f"🧩 {_opt_head(state, _day_label(t.get('date')))} · {escape(str(t.get('status') or 'waiting').replace('_', ' '))}"
             + (f" · {escape(t['why'])}" if t.get("why") else "") + (" · hold today" if t.get("hold") else "")]
    if o:
        s_, h = o["legs"][0], o["legs"][1]
        lines.append(f"open: {o['bias']} · SELL {_strike(s_)} @ {s_['entry']:.2f} (now {s_['last']:.2f}) · BUY {_strike(h)} @ {h['entry']:.2f} · "
                     f"{o['lots']} lot{'s' if o['lots'] != 1 else ''} · P&L <b>{inr(o['pnl'], sign=True)}</b> · stop {o['stop_premium']:.2f}"
                     f"{' · breakeven stop on' if o.get('be_active') else ''} · target {o['target_value']:.2f}")
    else:
        lines.append("no spread open")
    lines.append(f"<i>today {inr(t.get('realised') or 0.0, sign=True)} realised · {t.get('trades', 0)} trade{'s' if t.get('trades', 0) != 1 else ''} · "
                 f"book {inr(s.get('equity'))} ({(s.get('return') or 0):+.1f}%)</i>")
    lines.append("Reply <b>opt close</b> to close the open spread · <b>opt hold</b> for no new entries today.")
    return "\n".join(lines)


def options_alert(a: dict) -> Optional[str]:
    """The Telegram text of one alert the book recorded ({kind, t, text, data}); None for a kind with no builder (the
    plain text goes then)."""
    k, d = a.get("kind"), a.get("data") or {}
    tag = a.get("tag") or "paper"
    try:
        if k == "entry" and d.get("sold"):
            return options_bias({**d, "opened_t": d.get("opened_t") or a.get("t")}, tag)
        if k == "exit" and d.get("sold"):
            return options_exit(d.get("why") or "closed", d, tag)
        if k == "no_trade":
            return options_no_trade(d)
        if k == "summary" and d.get("row"):
            return options_summary(d["row"], d["book"], tag)
        if k == "chain_disagrees":
            return options_chain_disagrees(d, tag)
        if k == "daily_loss":
            return options_daily_loss(d, tag)
        if k in ("event_day", "skipped"):
            return options_skipped(k, d, tag)
        if k == "vix_half":
            return options_vix_half(d, tag)
        if k in ("no_trade_size", "no_trade_strikes"):
            return options_no_size(k, d, tag)
        if k == "no_data":
            return options_no_data(d, tag)
    except (KeyError, TypeError, ValueError):
        pass
    return f"🧩 {_opt_head(tag, a.get('t'))} · {escape(a.get('text') or '')}"
