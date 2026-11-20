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


AI_MARK = "<i>AI ·</i> "                    # 🤖 starts every model-written line (code-written lines carry nothing)
AI_HEAD = "[AI] "                          # 🤖 opens the bold header of a message the model wrote entirely


def ai_mark(text: str) -> str:
    """One model-written line as Telegram shows it: "<i>AI ·</i> <i>text</i>" (the text escaped)."""
    return f"{AI_MARK}<i>{escape(text)}</i>"


def ai_line(note: Optional[dict]) -> str:
    """🤖 The last line of a filing / deal alert when the AI has a note for that source: "🤖 <i>AI ·</i> <i>high · …</i>"."""
    return f"\n🤖 {ai_mark(note['severity'] + ' · ' + note['text'])}" if note else ""


def filing_alert(f: dict, why: str, note: Optional[dict] = None) -> str:
    from backend.ingest.filings import LABELS
    d = datetime.fromisoformat(f["dt"])
    text = f["text"] if len(f["text"]) <= 400 else f["text"][:397] + "…"
    lines = [f"{FILING_ICONS.get(f['kind'], '📰')} <b>{escape(f['symbol'])}</b> · {LABELS[f['kind']]} "
             f"<i>({escape(why)})</i>", escape(text),
             f"<i>{escape(f['category'])} · {d.day} {d:%b}, {ampm(d)}</i>"]
    if f.get("url", "").startswith("https://"):
        lines[-1] += f' · <a href="{escape(f["url"], quote=True)}">filing</a>'
    return "\n".join(lines) + ai_line(note)


def deal_alert(symbol: str, icon: str, item: dict, why: str, note: Optional[dict] = None) -> str:
    tail = " Holds it at tier B: no A alerts until it clears." if icon == "🚩" else ""
    return (f"{icon} <b>{escape(symbol)}</b> · {escape(item['label'])} <i>({escape(why)})</i>\n"
            f"{escape(item['text'])}.{tail}") + ai_line(note)


def note_alert(note: dict, where: str, skip_hint: Optional[str] = None) -> str:
    """🤖 The separate message for a high note the filing / deal alert did not carry: the symbol and the category, the
    AI line, then the part code knows (held in Real (+x%) / in tomorrow's plan at #n / on your watchlist, the skip hint),
    the source time and a link to the filing."""
    cat = (note.get("category") or note.get("kind") or "note").replace("_", " ")
    lines = [f"📰 <b>{AI_HEAD}{escape(note['symbol'])} · {escape(cat)}</b>",
             f"🤖 {ai_mark(note['severity'] + ' · ' + note['text'])}"]
    tail = [escape(where, quote=False)]
    if skip_hint:
        tail.append(escape(skip_hint, quote=False))
    try:
        d = datetime.fromisoformat(note["source_at"])
        tail.append(f"{d.day} {d:%b}" + (f", {ampm(d)}" if len(note["source_at"]) > 10 else ""))
    except (KeyError, TypeError, ValueError):
        pass
    if (note.get("url") or "").startswith("https://"):
        tail.append(f'<a href="{escape(note["url"], quote=True)}">filing</a>')
    lines.append(f"<i>{' · '.join(tail)}</i>")
    return "\n".join(lines)


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
    views = {o["symbol"]: o for o in (m or {}).get("orders") or [] if o.get("action") == "BUY"}
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
            mo = views.get(o["symbol"]) or {}                                  # 🤖 AI view: the morning row (size, rating)
            qty, was = (mo["qty_ai"], f" ({mo['size_x']:g}× · was {mo['qty_rule']})") if mo.get("qty_ai") and mo["qty_ai"] != mo.get("qty_rule") else (o["qty"], "")
            lines.append(f"🟢 BUY <b>{escape(o['symbol'])}</b> {qty}{was} · stop {_rup(o['stop'])} · ~{_rup(qty * o['close'])}{tag}")
            flag = ((m or {}).get("ai_flags") or {}).get(o["symbol"])        # 🤖 suggestions: the AI's flag on a planned name
            if flag:
                lines.append(f"   🤖 {ai_mark(flag)}")
            av = ai_view_line(mo.get("ai_view"))
            if av:
                lines.append(f"   · {av}")
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


def real_sent(res: dict, at: str, gate: bool = False) -> str:
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
    buys = [o["symbol"] for o in res["sent"] if o["action"] == "BUY"]
    if buys and gate:                                      # the EXIT gate is on: accepted ≠ filled, ask for the owner's eyes
        lines.append("Once you see the fill, reply " + " · ".join(f"<b>filled {escape(s)}</b>" for s in buys)
                     + " · no EXIT goes out until you do")
    return "\n".join(lines)


def real_stop(h: dict, at: str) -> str:
    if h.get("error"):
        return (f"{REAL} · ⚠️ STOP HIT, EXIT FAILED · {at}\n<b>{escape(h['symbol'])}</b> {h['qty']} at {_rup(h['price'])}: "
                f"{escape(h['error'])}. Exit it yourself.")
    return (f"{REAL} · STOP HIT · EXIT SENT · {at}\n🛑 <b>{escape(h['symbol'])}</b> {h['qty']} · traded {_rup(h['price'])} "
            f"≤ {escape(h['why'])} · net {_signed(h['net'])}")


def real_unconfirmed(h: dict) -> str:
    """The EXIT gate held an EXIT: the buy was never confirmed, so nothing was sent (once a day per stock)."""
    sym = escape(h["symbol"])
    what = "stop hit" if str(h.get("why") or "").startswith("stop") else f"EXIT due ({escape(h.get('why') or 'exit')})"
    return (f"⚠️ <b>REAL</b> · <b>{sym}</b> {what} but the buy was never confirmed · no EXIT sent · confirm it "
            f"(reply <b>filled {sym}</b>) or remove it (<b>unfilled {sym}</b>)")


def real_confirm_nudge(still: list, gate: bool = False) -> str:
    """real.confirm_time without an API key: the buys still not confirmed by hand (no call made, nothing sent)."""
    names = " · ".join(f"<b>filled {escape(s)}</b>" for s in still)
    return f"⏳ <b>REAL</b> · {len(still)} fill{'s' if len(still) > 1 else ''} not confirmed yet · reply {names} · no EXIT goes out until you do"


def real_fills_checked(res: dict) -> str:
    """The fill check confirmed one or more buys (read-only; nothing was sent)."""
    names = ", ".join(f"<b>{escape(s)}</b>" for s in res.get("confirmed") or [])
    left = res.get("still") or []
    return (f"✅ <b>REAL</b> · fill check: {names} confirmed filled · {res['n_confirmed']} of {res['total']} confirmed"
            + (f" · still unconfirmed: {', '.join(escape(s) for s in left)}" if left else ""))


def real_hand(text: str) -> str:
    """A hand correction (sold outside the app, a closed trade added, a trade marked as never filled, a fill confirmed or
    removed): the activity line. Nothing was sent."""
    return f"{REAL} · ✎ {escape(text)} · nothing sent"


def real_nologin(at: str) -> str:
    return (f"{REAL} · 🚨 STOPS NOT WATCHED · {ampm(at)}\nKite is not logged in and real positions are open. "
            "Log in to Kite now: the first check after the login exits any position whose stop was touched meanwhile.")


def tb_name(variant: str, top: Optional[int] = None) -> str:
    base = {"E": "E+ · half filter"}.get(variant, variant)
    if variant == "E" and top:
        return f"{base}, top {top}, late trail"
    return f"{base}, top {top}" if top else base


def trail_raised(where: str, sym: str, stop: float, best: float, entry: float) -> str:
    """E+'s late trail moved a stop up (after the close; it applies from the next session)."""
    return (f"{where} · <b>E+ · {escape(sym)}</b> trail stop raised to <b>₹{stop:,.2f}</b> (best close ₹{best:,.2f}, "
            f"+{(best / entry - 1) * 100:.0f}% over the entry {_rup(entry)}) · from the next session"
            + ("\nWatched by the app like any stop: an EXIT goes to Tradetron automatically if it is hit (while real "
               "trading is on). A stop sent to Tradetron with the BUY is not moved." if where == REAL else ""))


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
    vwap = o.get("vwap_exit", True)
    stop = (f"stop {o['stop_premium']:.2f} (+{o['stop_pct']:g}%)" + (f" or a 5-min close {'under' if bull else 'over'} VWAP" if vwap else "") + " · "
            f"target {o['target_value']:.2f} ({o.get('target_pct', 60):g}%) · worst case {inr(-o['worst_case'], sign=True)}")
    from backend.options.book import exits_text
    stop += f" · {exits_text(o['stop_pct'], vwap)}"
    dim = [f"margin ~{inr(o['margin_est'])}"]
    if o.get("sessions") is not None:
        dim.append("expiry day" if o["sessions"] == 0 else f"{o['sessions']} session{'s' if o['sessions'] != 1 else ''} to expiry")
    if o.get("vix"):
        dim.append(f"VIX {o['vix']:.1f}")
    out = [f"🧭 {_opt_head(tag, f'bias {bias}', o.get('opened_t'))}", escape(price), legs, escape(stop), f"<i>{escape(' · '.join(dim))}</i>"]
    if o.get("chain_line"):
        out.append(f"<i>chain: {escape(o['chain_line'])}</i>")
    n = o.get("n") or 1
    if n > 1:
        of = f" ({n} of {o['max_trades']})" if o.get("max_trades") else ""
        if o.get("kind") == "same":
            why = f"same bias · {o['entry_why']}" if o.get("entry_why") else "same bias · back over the range and VWAP after a stop"
        else:
            why = "the bias flipped"
        late = (o.get("window") or {}).get("reentry_until")
        out.insert(1, f"<i>re-entry{of} · {escape(why)}" + (f" · re-entries to {ampm(late)}" if late and o.get("late") else "") + "</i>")
    return "\n".join(out)


def options_afternoon(o: dict, tag: str = "paper") -> str:
    """🧩 The afternoon pair sold: both strikes, the credit, the day's range, the exits (no VWAP); then the legs with
    the hedges and the stops, the worst case, the margin."""
    sides = o.get("sides") or []
    names = " / ".join(_strike(x["sold"]) for x in sides)
    head = (f"🧩 {_opt_head(tag, 'afternoon sell', o.get('opened_t'))} · {names} · credit {inr(o.get('credit_rs'))} · range {o.get('range_pct') or 0:.1f}% · "
            f"{escape(o.get('exits') or '')}")
    lots = o.get("lots") or 1
    legs = [f"SELL <b>{_strike(x['sold'])}</b> @ {x['sold']['entry']:.2f} · BUY {_strike(x['hedge'])} @ {x['hedge']['entry']:.2f} · stop {x['stop_premium']:.2f} · "
            f"target {x['target_value']:.2f}" for x in sides]
    n, of = o.get("n") or 1, o.get("max_trades")
    dim = [f"{lots} lot{'s' if lots != 1 else ''} each side", f"worst case {inr(-(o.get('worst_case') or 0), sign=True)}", f"margin ~{inr(o.get('margin_est'))}",
           f"trade {n} of {of}" if of else "", "non-directional · no VWAP exit"]
    return "\n".join([head, *legs, f"<i>{escape(' · '.join(x for x in dim if x))}</i>"])


EXIT_ICON = {"target": "🎯", "premium stop": "🛑", "vwap stop": "🛑", "breakeven stop": "🛑", "time exit": "⏱", "hard exit": "⏱",
             "kill": "🔴", "closed by you": "✋"}


def options_exit(kind: str, t: dict, tag: str = "paper") -> str:
    """🎯 / 🛑 / ⏱ the spread closed: the strikes, the exit value, the P&L, the time held, the breakeven stop when it was
    armed, 'at the last known mid' when no quote was there. `t` is the trade row."""
    from backend.options.book import EXIT_LABEL
    s, h = t["sold"], t["hedge"]
    label = EXIT_LABEL.get(kind, kind)
    if t.get("kind") == "afternoon":
        label = f"afternoon {t['sold']['type']} · {label}"
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
    if d.get("afternoon"):
        nxt = f" · afternoon sell looks at {ampm(d['afternoon'])}" + nxt
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
    if o and o.get("kind") == "afternoon":
        lines.append("open: afternoon pair · " + " · ".join(f"SELL {_strike(x)} @ {x['entry']:.2f} (now {x['last']:.2f}) stop {x['stop']:.2f}"
                                                            for x in o["legs"] if x["leg"] == "sold")
                     + f" · {o['lots']} lot{'s' if o['lots'] != 1 else ''} each side · P&L <b>{inr(o['pnl'], sign=True)}</b> · target {o['target_value']:.2f}")
    elif o:
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


def ai_status(s: dict) -> str:
    """The `ai` command's reply: enabled / stopped, the models, the RAM in use, the queue, the breaker."""
    sw, m, srv, qu, b = s["switches"], s["models"], s["server"], s["queue"], s["breaker"]
    state = "⏹ full stop" if sw["stopped"] else ("on" if sw["enabled"] else "off")
    have = lambda k: f"{escape(m[k]['name'])} {'✓' if m[k]['present'] else 'missing'}"
    ram = f"{srv['ai_ram_mb'] / 1000:.1f} GB in use" if srv.get("ai_ram_mb") else ("nothing loaded" if s.get("url_ok") else "service unreachable")
    job = f" · running {escape(qu['job']['kind'])}" if qu.get("job") else ""
    lines = [f"🤖 <b>AI · {state}</b> · night {have('night')} · day {have('day')}",
             f"RAM {ram} · queue {qu['queued']} waiting{job} · today {qu['done_today']} done / {qu['failed_today']} failed · breaker {escape(b['text'])}",
             "Reply <b>ai stop</b> to unload the model and empty the queue · <b>ai start</b> to clear the stop."]
    return "\n".join(lines)


def ai_stopped(r: dict) -> str:
    """The `ai stop` reply: what was unloaded and freed."""
    freed = f"{r['ram_freed_mb'] / 1000:.1f} GB freed" if r.get("ram_freed_mb") else "nothing was loaded"
    return (f"⏹ <b>AI full stop</b> · {freed} · {r['skipped']} queued job(s) skipped"
            + (f" · unloaded {escape(', '.join(r['unloaded']))}" if r.get("unloaded") else "")
            + "\nThe container keeps idling (~50 MB). Reply <b>ai start</b> to clear the stop.")


def options_alert(a: dict) -> Optional[str]:
    """The Telegram text of one alert the book recorded ({kind, t, text, data}); None for a kind with no builder (the
    plain text goes then)."""
    k, d = a.get("kind"), a.get("data") or {}
    tag = a.get("tag") or "paper"
    try:
        if k == "entry" and d.get("sold"):
            return options_bias({**d, "opened_t": d.get("opened_t") or a.get("t")}, tag)
        if k == "afternoon" and d.get("sides"):
            return options_afternoon({**d, "opened_t": d.get("opened_t") or a.get("t")}, tag)
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


# --- 🤖 brief ---------------------------------------------------------------------------------------------------------------

def ai_brief(day: str, at: str, lines: list[str]) -> str:
    """☀️ [AI] The morning brief: the day and time, the model's lines (≤ 5, each marked "<i>AI ·</i>"), the footer that
    says where it came from."""
    head = f"☀️ <b>{AI_HEAD}Morning brief · {escape(_day_label(day))} · {ampm(at)}</b>"
    return "\n".join([head, *(AI_MARK + escape(ln, quote=False) for ln in lines), "<i>written from the plans and filings · no action taken</i>"])


# --- 🤖 ask ------------------------------------------------------------------------------------------------------------------

def ai_thinking(question: str) -> str:
    """The first reply to `ask <question>`, sent before the model runs."""
    return f"💬 <b>ask</b> {escape(question, quote=False)}\n<i>thinking…</i>"


def ai_ask_reply(row: dict) -> str:
    """💬 [AI] The answer: the question, '<i>AI ·</i> the answer' (an error carries no marker: the app wrote it), then
    what was read, the seconds and 'read-only'."""
    tail = [row["read"]] if row.get("read") else []
    if row.get("ms"):
        tail.append(f"{max(1, round(row['ms'] / 1000))} s")
    tail.append("read-only")
    body = escape(row.get("answer") or row.get("error") or "no answer", quote=False)
    mark = AI_MARK if row.get("answer") else ""                               # an error is the app's, not the model's
    return f"💬 <b>{AI_HEAD}ask</b> {escape(row['question'], quote=False)}\n{mark}{body} <i>({escape(' · '.join(tail), quote=False)})</i>"


# --- 🤖 suggestions (PR 3 · A) ----------------------------------------------------------------------------------------------

def _day_full(d: Optional[str], fmt: str = "%A") -> str:
    """'2026-11-17' -> 'Monday' (fmt "%a %d %b" -> 'Tue 17 Nov')."""
    try:
        return datetime.fromisoformat(d).strftime(fmt).replace(" 0", " ") if d else ""
    except ValueError:
        return d or ""


def _until(iso: Optional[str]) -> str:
    if not iso:
        return "no expiry"
    try:
        d = datetime.fromisoformat(iso)
        return f"until {ampm(d)}" if d.date() == datetime.now(d.tzinfo).date() else f"until {ampm(d)}, {d.day} {d:%b}"
    except ValueError:
        return "until ?"


def _sugg_line(s: dict) -> str:
    conf = f" · {s['confidence']}" if s.get("confidence") else ""
    why = f"<i>{escape(s.get('why') or '')}</i>" if s.get("why_source") == "code" else ai_mark(s.get("why") or "")
    return (f"<b>#{s['id']}</b> {s.get('icon', '🤖')} <b>{escape(s['title'])}</b>{conf}\n"
            f"   {why} · {escape(_until(s.get('expires_at')))}")


def ai_suggestions(rows: list, for_day: Optional[str] = None) -> str:
    """The evening list "🤖 N suggestions for Monday": numbered by id, the kind icon, the title, the why, until when."""
    n = len(rows)
    head = f"🤖 <b>{AI_HEAD}{n} suggestion{'s' if n != 1 else ''}</b>" + (f" for {_day_full(for_day)}" if for_day else "")
    lines = [head] + [_sugg_line(s) for s in rows]
    lines.append("Reply <b>accept N</b> · <b>dismiss N [wrong|knew|later]</b> · <b>why N</b> (the evidence) · <b>sugg</b> (the list). "
                 "Nothing happens without your reply; the model only proposes.")
    return "\n".join(lines)


def ai_suggestion_list(rows: list) -> str:
    """The `sugg` reply: the open suggestions, or a line saying there are none."""
    if not rows:
        return "🤖 No open suggestions."
    return ai_suggestions(rows)


def ai_suggestion_detail(s: dict) -> str:
    """The `why N` reply: the title, the why, every evidence line, the action's path and the state."""
    lines = [_sugg_line(s)]
    for e in s.get("evidence") or []:
        url = f' · <a href="{escape(e["url"], quote=True)}">source</a>' if (e.get("url") or "").startswith("https://") else ""
        lines.append(f"{e.get('icon', '•')} {escape(e['text'])}{url}")
    a = s.get("action") or {}
    if a.get("path") and a["path"] != "ui":
        lines.append(f"<i>accept → {escape(a['path'])} (the same code path as the hand action)</i>")
    elif a.get("route"):
        lines.append(f"<i>accept → opens {escape(a['route'])}: {escape(a.get('hint') or '')}</i>")
    else:
        lines.append("<i>note only · nothing to run</i>")
    if s.get("status") != "open":
        lines.append(f"<i>{escape(s['status'])}" + (f" · {escape(s['reason'])}" if s.get("reason") else "") + "</i>")
    return "\n".join(lines)


def ai_accept_confirm(s: dict) -> str:
    """The two-step accept's question: the action's confirm text and the `yes N` reply."""
    a = s.get("action") or {}
    return (f"🤖 <b>#{s['id']} · {escape(s['title'])}</b>\n{escape(a.get('confirm_text') or 'Run it?')}\n"
            f"Reply <b>yes {s['id']}</b> within 10 minutes to go ahead; anything else leaves it open.")


def ai_accepted(s: dict) -> str:
    res = (s.get("action_result") or {})
    ran = "the action ran" if res.get("ran") else "noted (nothing to run)"
    return f"✅ <b>#{s['id']} accepted</b> · {escape(s['title'])} · {ran}."


def ai_scorecard(sc: dict, month: str) -> str:
    """The monthly scorecard line (sent on the first night batch of the next month)."""
    t = sc["totals"]
    kinds = " · ".join(f"{k} {v['made']}" for k, v in sc["kinds"].items() if v["made"])
    best = f" · best kind {escape(t['best_kind'])}" if t.get("best_kind") else ""
    return (f"🤖 <b>Suggestions · {escape(_month(month))}</b> · {t['made']} made · {t['accepted']} accepted · {t['dismissed']} dismissed · "
            f"{t['expired']} expired{best}\n<i>{escape(kinds) or 'none'} · accepted outcome {inr(t['accepted_outcome_rs'], sign=True)} · "
            f"dismissed outcome {inr(t['dismissed_outcome_rs'], sign=True)}</i>")


def _month(m: str) -> str:
    try:
        return datetime.strptime(m, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return m


# --- 🤖 why (PR 3 · B) ---------------------------------------------------------------------------------------------------------

WHY_ICON = {"real": "💰", "paper": "📝", "options": "🧩", "held": "📦"}


WHY_BOOK = {"real": "Real", "paper": "Paper", "options": "Options", "held": "Held"}


def _why_trades(n: dict) -> list:
    """The trade rows one why line covers: `trades` (a stock closed in several books, merged) or the single `trade`."""
    data = n.get("data") if isinstance(n.get("data"), dict) else {}
    many = n.get("trades") or data.get("trades")
    return many or [n.get("trade") or data.get("trade") or {}]


def _why_head(n: dict) -> str:
    """'💰 <b>RT</b> · stop hit · −₹620' for one trade; '💰📝 <b>SYM</b> · stop hit · Real −₹1,702 · Paper −₹1,751' when
    several books closed the stock the same day (the books' nets on one line)."""
    ts = _why_trades(n)
    if len(ts) == 1:
        t = ts[0]
        net = f" · {inr(t['net'], sign=True)}" if t.get("net") is not None else ""
        return f"{WHY_ICON.get(n['book'], '📦')} <b>{escape(n['symbol'])}</b> · {escape(str(t.get('label') or ''))}{net}"
    icons = "".join(dict.fromkeys(WHY_ICON.get(t.get("book"), "📦") for t in ts))
    label = " / ".join(dict.fromkeys(str(t.get("label") or "") for t in ts if t.get("label")))
    nets = " · ".join(f"{WHY_BOOK.get(t.get('book'), t.get('book') or '?')} {inr(t['net'], sign=True)}" for t in ts if t.get("net") is not None)
    return f"{icons} <b>{escape(n['symbol'])}</b>" + (f" · {escape(label)}" if label else "") + (f" · {nets}" if nets else "")


def ai_why_evening(rows: list, day: str) -> str:
    """🧾 [AI] Today's exits, explained: one line per stock closed today (the book or books, the name, how it ended, the
    net per book) with the AI's "likely cause" under it."""
    lines = [f"🧾 <b>{AI_HEAD}Today's exits, explained</b> · {_day_full(day, '%a %d %b')}"]
    for n in rows:
        lines.append(f"{_why_head(n)}\n   🤖 {ai_mark(n['text'])}")
    lines.append("<i>written from the trade rows, the day's bars and the filings · no action taken</i>")
    return "\n".join(lines)


# --- 🤖 AI view --------------------------------------------------------------------------------------------------------------------

VIEW_ICON = {"buy": "🟢", "wait": "🟡", "avoid": "🔴", "hold": "🟢", "trim": "🟡", "exit-watch": "🔴"}


def ai_view_line(v: Optional[dict]) -> str:
    """'🤖 AI 82 buy · <i>AI ·</i> <i>reason 1</i>' (the dry run's line under a BUY: the score and verdict are code's
    chip, the reason the model's line); '' without a view."""
    if not v or v.get("score") is None:
        return ""
    reason = (v.get("reasons") or [""])[0]
    return f"🤖 AI {v['score']} {escape(v['verdict'])}" + (f" · {ai_mark(reason)}" if reason else "")


def ai_view_night(night: dict) -> str:
    """🌙 AI view · tonight: how many names were rated and the verdict counts (one line, sent after the night batch)."""
    by = night.get("by_verdict") or {}
    order = ("buy", "wait", "avoid", "hold", "trim", "exit-watch")
    counts = " · ".join(f"{VIEW_ICON[k]} {by[k]} {k}" for k in order if by.get(k))
    extra = (f" · {night['failed']} failed" if night.get("failed") else "") + (f" · {night['capped']} over the cap" if night.get("capped") else "")
    at = ampm(night["at"][11:16]) if night.get("at") else ""
    return (f"🌙 <b>AI view · tonight</b> · {night.get('n', 0)} rated{extra}" + (f"\n{counts}" if counts else "")
            + f"\n<i>ratings only · the plan, the sizes and the stops are as the rules made them{' · ' + at if at else ''}</i>")


def ai_view_exit_watch(x: dict) -> str:
    """🤖 SYM · exit-watch: a held name closed under the level its view said the setup fails below (a flag only)."""
    return (f"🤖 <b>{escape(x['symbol'])}</b> · exit-watch · closed {rs(x['close'])} under {rs(x['fails_below'])} (setup fails below)"
            f"\n<i>a flag only: nothing is sold and the stop stays as it is · {escape(x.get('book') or '')}</i>")


def ai_view_week(w: dict, rec: dict) -> str:
    """📊 AI view · week n: the week's buy / avoid hit@20 vs the plan baseline, the streak and the unlock notice."""
    n = w["week"].split("-W")[-1].lstrip("0")
    pct = lambda v: "–" if v is None else f"{v:g}%"
    u = rec.get("unlock") or {}
    tail = ("🔓 <b>unlocked</b>: the three AI view switches may now be turned on in Settings (each stays off until you do)"
            if u.get("unlocked") else f"🔒 locked · {u.get('streak', 0)} of {u.get('need', 4)} weeks in a row")
    return (f"📊 <b>AI view · week {escape(n)}</b> · buy hit@20 {pct(w['buy_hit20'])} · avoid {pct(w['avoid_hit20'])} · "
            f"plan {pct(w['baseline20'])} · {'beat' if w['beat'] else 'did not beat'} the plan\n{tail}")


def ai_week_story(week: str, lines: list, at: Optional[str] = None, rows: Optional[int] = None) -> str:
    """📖 [AI] Week 46 · the story: the model's observations (≤ 3, each marked "<i>AI ·</i>"), then a code footer (how
    many trade rows they were checked against, when they were written, 12-hour)."""
    n = str(week).split("-W")[-1].lstrip("0") or str(week)
    foot = ["checked against " + _n(rows, "trade row") if rows is not None else None, f"written {at}" if at else None, "no action taken"]
    return "\n".join([f"📖 <b>{AI_HEAD}Week {escape(n)} · the story</b>", *(AI_MARK + escape(str(ln), quote=False) for ln in lines),
                      "<i>" + " · ".join(x for x in foot if x) + "</i>"])


# --- 🌙 day summary (one close-of-day message across the books) ----------------------------------------------------------------

def _n(n: int, word: str, plural: Optional[str] = None) -> str:
    """'1 trade' · '3 trades' (`plural` for irregular words)."""
    return f"{n} {word if n == 1 else plural or word + 's'}"


def _today(v: Optional[float], marks_at: Optional[str] = None) -> str:
    """'today −₹1,702'; a book not yet marked today says when it is ('today at the 6:30 PM mark')."""
    return f"today {inr(v, sign=True)}" if v is not None else f"today at the {ampm(marks_at or '18:30')} mark"


def _sum_real(r: dict) -> str:
    stops = r.get("stops") or []
    bits = [f"💰 Real · {inr(r['value'])}", _today(r.get("today"))]
    if stops:
        bits.append(f"{_n(len(stops), 'stop')} hit ({escape(', '.join(stops))})")
    bits.append(f"{_n(r.get('failed') or 0, 'order')} failed")
    tm = r.get("tomorrow")
    if tm:
        bits.append(f"tomorrow {_n(tm.get('buys', 0), 'BUY')} {_n(tm.get('exits', 0), 'EXIT')}")
    return " · ".join(bits)


def _sum_paper(p: dict) -> str:
    bits = [f"📝 Paper {escape(p.get('name') or 'E+')} · {inr(p['value'])}", _today(p.get("today"), p.get("marks_at"))]
    if p.get("stops"):
        bits.append(f"{_n(p['stops'], 'stop')} hit")
    return " · ".join(bits)


def _sum_options(o: dict) -> str:
    n = o.get("trades") or 0
    bits = [f"🧩 Options · {escape(o.get('tag') or 'paper')} · {inr(o['value'])}", _today(o.get("today"))]
    bits.append((_n(n, "trade") + (" (incl. the afternoon pair)" if o.get("afternoon") else "")) if n else "no trade")
    if o.get("result"):
        bits.append(escape(str(o["result"])))
    return " · ".join(bits)


def _sum_ai(a: dict) -> str:
    if a.get("ran"):
        return f"🤖 AI · {_n(a.get('notes', 0), 'note')} · {_n(a.get('suggestions', 0), 'suggestion')} · {a.get('rated', 0)} rated"
    return f"🤖 AI · tonight after {ampm(a.get('after') or '18:30')}: {', '.join(a.get('parts') or ['notes'])}"


DAY_SUMMARY_ORDER = (("real", _sum_real), ("paper", _sum_paper), ("options", _sum_options), ("ai", _sum_ai))


def day_summary(parts: dict, day: str) -> Optional[str]:
    """🌙 Day summary · Mon 16 Nov: one line per book that had anything today, in a fixed order (Real, Paper, Options,
    the AI), the value and the day's P&L always in the same place; a book with nothing is
    left out (`parts` has no key for it; paper is a list, one per account). None when no book has a line."""
    lines = []
    for key, line in DAY_SUMMARY_ORDER:
        part = parts.get(key)
        for p in (part if isinstance(part, list) else [part] if part else []):
            lines.append(line(p))
    if not lines:
        return None
    return "\n".join([f"🌙 <b>Day summary · {_day_full(day, '%a %d %b')}</b>", *lines])


def quiet_flushed(n: int) -> str:
    """🤖 The one line before the AI messages held in the quiet hours."""
    return f"🤖 held overnight · {_n(n, 'message')}"


# --- 💡 Tips: a Telegram channel's calls (backend/tips); kind "tips", never held ------------------------------------------

TIP = "💡"


def _tip_t(iso_or_dt) -> str:
    """'9:43 AM' (today) / 'Mon 2:00 PM' (an earlier day) in IST."""
    from backend.config import IST
    dt = iso_or_dt if isinstance(iso_or_dt, datetime) else datetime.fromisoformat(str(iso_or_dt))
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST)
    return ampm(dt) if dt.date() == datetime.now(IST).date() else f"{dt:%a} {ampm(dt)}"


def _expiry_label(iso: Optional[str]) -> str:
    if not iso:
        return ""
    from datetime import date
    d = date.fromisoformat(iso[:10])
    return f"{d:%a} {d.day} {d:%b}"


def tips_hint_body(h: dict) -> str:
    """'BUY BANKNIFTY 50300 PE · near 250–270 when comes · SL 190 · Tgt 320 / 350 / 400++ · expiry Wed 19 Nov <i>AI</i>'."""
    from backend.tips import decode as dc
    from backend.tips import plan as pl
    p = h.get("plan") or {}
    dec = h.get("decoded") or {}
    ai = any(v == "ai" for v in dec.values()) or h.get("expiry_src") == "ai"
    bits = [escape(dc.title(h)), escape(pl.entry_text(h.get("entry") or {})), escape(pl.stop_text(h.get("stop") or {})),
            escape(pl.targets_text(h.get("targets") or [], bool(h.get("open_ended"))))]
    if h.get("expiry"):
        bits.append("expiry " + _expiry_label(h["expiry"]) + (" (computed)" if h.get("expiry_src") == "computed" else ""))
    out = " · ".join(bits) + (" <i>AI</i>" if ai else "")
    if not p.get("ok", True):
        out += f" · <b>no plan: {escape(p.get('why') or '')}</b>"
    return out


def tips_hint(h: dict, ltp: Optional[float] = None) -> str:
    """💡 Tip #12 · channel · 9:43 AM / the call / the plan / the replies (the mock's first card)."""
    n = h["id"]
    lines = [f"{TIP} <b>Tip #{n} · {escape(h.get('channel') or '')} · {_tip_t(h['posted_at'])}</b>", tips_hint_body(h)]
    p = h.get("plan") or {}
    if p.get("text"):
        lines.append(f"<i>plan: {escape(p['text'])}</i>")
    if not h.get("tradingsymbol"):
        lines.append(f"<i>not tracked: {escape(h.get('untracked_why') or 'no instrument')}</i>")
    elif h.get("untracked_why"):
        lines.append(f"<i>quotes start after you {escape(h['untracked_why'])}</i>")
    size = "" if (h.get("size_hint") or {}) else f" · size not stated → <b>take {n} N</b>"
    now = f" · now {ltp:g}, waiting" if ltp is not None else ""
    lines.append(f"reply <b>take {n}</b> · <b>take {n} 2</b> (lots) · <b>skip {n}</b> · <b>tip {n}</b>{size}{now}")
    return "\n".join(lines)


def tips_taken(h: dict, now: datetime) -> str:
    """✅ #12 taken · 1 lot at 262 · 9:51 AM · paper · stop 190 · T1 320 (or "waiting for the entry")."""
    from backend.tips import plan as pl
    pos = h.get("pos") or {}
    tg = (h.get("plan") or {}).get("targets") or []
    size = ("½ lot" if pos.get("half") and pos.get("lots") == 1 else f"{pos.get('lots', 1):g} lot" + ("s" if pos.get("lots", 1) != 1 else ""))
    run = pos.get("run") or {}
    if pos.get("state") == "open":
        head = f"✅ <b>#{h['id']} taken · {size} at {pl.fmt(pos.get('entry_px'))} · {ampm(now)}</b>"
    else:
        head = f"✅ <b>#{h['id']} taken · {size} · {ampm(now)}</b> · waiting for the entry ({escape(pl.entry_text(h.get('entry') or {}))})"
    stop = f"stop {pl.fmt(run.get('stop'))}" if run.get("stop") is not None else escape(pl.stop_text((h.get("plan") or {}).get("stop") or {}))
    return head + " · paper · " + stop + (f" · T1 {pl.fmt(tg[0])}" if tg else "")


def tips_line(hid: int, text: str, now: datetime, note: str = "") -> str:
    """💡 #12 · <text> · 10:08 AM (one line for each step the book took)."""
    return f"{TIP} #{hid} · {text} · {ampm(now)}" + (f" · {escape(note)}" if note else "")


def tips_followup(h: dict, text: str, line: str, now: datetime, act: Optional[dict] = None) -> str:
    """💡 #12 · channel: "2 targets done" · <what code did> · 10:08 AM · auto, as you set."""
    quote = escape((text or "").strip().replace("\n", " ")[:80])
    auto = " · auto, as you set" if (line or "").startswith("followed") else ""
    return f'{TIP} #{h["id"]} · channel: "{quote}" · {escape(line or "")} · {ampm(now)}{auto}'


def tips_status(s: dict) -> str:
    """tips → 1 active (#12 +₹2,148) · 1 waiting (#13) · today 2 hints · 30 d: 7 hints · T1 first 4 · stop 2 · no fill 1 · avg +0.9 R."""
    hs = s.get("hints") or []
    act = [h for h in hs if h.get("status") == "active"]
    wait = [h for h in hs if h.get("status") in ("new", "waiting", "paused")]
    bits = []
    if act:
        bits.append(f"{len(act)} active (" + ", ".join(f"#{h['id']}" + (f" {inr(h['pnl'], 0, True)}" if h.get("pnl") is not None else "") for h in act) + ")")
    if wait:
        bits.append(f"{len(wait)} waiting (" + ", ".join(f"#{h['id']}" for h in wait) + ")")
    if not bits:
        bits.append("nothing open")
    c = (s.get("cards") or {})
    today = (c.get("open") or {}).get("today") or {}
    bits.append(f"today {today.get('hints', 0)} hints")
    r = c.get("channel30") or {}
    avg = r.get("avg_r")
    bits.append(f"30 d: {r.get('hints', 0)} hints · T1 first {r.get('t1_first', 0)} · stop {r.get('stop_first', 0)} · no fill {r.get('no_fill', 0)}"
                + (f" · avg {avg:+.1f} R" if avg is not None else ""))
    rd = s.get("reader") or {}
    return "<b>tips</b> → " + " · ".join(bits) + f"\nreader {escape(rd.get('state') or '?')}" + (f" · {escape(rd['reason'])}" if rd.get("reason") else "")


def tips_card(h: dict) -> str:
    """tip N: the card: the raw post, the decoded call, the plan, the status, the last events."""
    from backend.tips import plan as pl
    lines = [f"{TIP} <b>#{h['id']} {escape(h.get('title') or '')}</b> · {escape(h.get('status') or '')}"
             + (f" · {escape(h['ended'])}" if h.get("ended") else "") + f" · {escape(h.get('channel') or '')} · {_tip_t(h['posted_at'])}",
             "<i>" + escape((h.get("raw") or "")[:400]) + "</i>", tips_hint_body(h)]
    p = h.get("plan") or {}
    if p.get("text"):
        lines.append("plan: " + escape(p["text"]))
    pos = h.get("pos")
    if h.get("taken") and pos:
        lines.append(f"paper: {escape(pos.get('state') or '')} · entry {pl.fmt(pos.get('entry_px'))} · P&L {inr(h.get('pnl'), 0, True)}")
    elif h.get("pnl") is not None:
        lines.append(f"at the plan: {inr(h['pnl'], 0, True)} per lot")
    if not h.get("tracked"):
        lines.append(f"not tracked: {escape(h.get('untracked_why') or '')}")
    for e in (h.get("events") or [])[-5:]:
        lines.append(f"{_tip_t(e['t'])} · {escape(e['text'])}")
    n = h["id"]
    if h.get("status") in ("new", "waiting", "active", "paused"):
        lines.append(f"reply <b>take {n}</b> · <b>take {n} 2</b> · <b>skip {n}</b> · <b>exit {n}</b> · <b>trail {n} &lt;px&gt;</b> · <b>pause {n}</b>")
    return "\n".join(lines)


def tips_channels(channels: list, reader: dict) -> str:
    return f"{TIP} <b>Tips channels</b>: " + (", ".join(escape(c) for c in channels) or "none") + \
        f"\nreader {escape(reader.get('state') or '?')}" + (f" · {escape(reader['account_masked'])}" if reader.get("account_masked") else "") + \
        "\nchange them on the Tips page (Channels…) or Settings › tips.channels"
