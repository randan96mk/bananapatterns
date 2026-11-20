"""🏠 Home · 💡 Tips · Kite (GET /api/home/tips, stream topic `home_tips`): the two cards, the strip, the working Kite rows
(open with P&L, armed with the distance), today's calls with every outcome, the checks, the record, the Next up times,
mode off, the route, Needs a look naming the hint and the hand, and the phone-safe markup of the block."""

import re
from datetime import date
from pathlib import Path

from backend.tips import book as bk
from backend.tips import home as th
from backend.tips import kite_check as kc
from backend.tips import kite_orders as ko
from tests.test_tips_api import api  # noqa: F401
from tests.test_tips_kite_auto import CALL1, CALL2, CALL3, S82200, S82300, S82400, auto_setup, post, pushed, t19, tick  # noqa: F401
from tests.tips_world import at

ROOT = Path(__file__).resolve().parent.parent
KEYS = {"enabled", "mode", "dry", "kill", "window", "product", "caps", "reader", "card", "channel30", "rows", "today", "checks", "record",
        "next", "live", "at"}


def _day():
    """19 Nov in auto: #1 LIMIT 540 fills then T1 600 (closed), #2 SL-M 370 rests (armed), #3 SL-M 306 fills (open)."""
    c, w, s = auto_setup(kite_auto_max_open=3)
    w.prices.update({S82200: 560.0, S82300: 309.0, S82400: 280.0})
    h1 = post(c, w, s, 101, "12:50", CALL1)["hint_id"]
    tick(c, w, {S82200: 536.0}, "12:58")
    tick(c, w, {S82200: 610.0}, "13:20")
    h2 = post(c, w, s, 105, "13:52", CALL2)["hint_id"]
    tick(c, w, {S82300: 337.0}, "13:53")
    h3 = post(c, w, s, 108, "14:04", CALL3)["hint_id"]
    tick(c, w, {S82400: 312.0}, "14:05")
    tick(c, w, {S82400: 330.0}, "14:10")
    return c, w, s, h1, h2, h3


def test_cards_rows_chips_checks_record_from_a_seeded_day(pushed):  # noqa: F811
    c, w, s, h1, h2, h3 = _day()
    c.tips.kite_mode = "dry"                                              # the seeded day ran through the simulator
    d = th.home_tips(c, t19("14:12"))
    assert set(d) == KEYS and d["mode"] == "dry" and d["dry"] and d["live"] and d["window"] == "9:20 AM – 3:00 PM"
    rows = {r["id"]: r for r in d["rows"]}
    assert list(rows) == [h3, h2]
    o = rows[h3]                                                          # open: P&L at the last stored quote, stop and target distance
    assert o["state"] == "entered" and o["tag"].endswith("open · 1 lot") and o["pnl"] == (330 - 306) * 20
    assert o["facts"] == "now 330 · stop 250 is −24.2 % · target 380 is +15.2 %"
    assert o["detail"] == "entry 306 · 2:05 PM · auto · stop resting in the dry run · targets watched by the app" and o["buttons"] == ["trail", "exit"]
    a = rows[h2]                                                          # armed: the distance to the level, one lot
    assert a["tag"].endswith("armed · SL-M 370 resting") and a["pnl"] is None
    assert a["facts"] == "now 337 · level 370 is 9.8 % away · 1 lot"
    assert a["detail"] == "1:52 PM · auto · the level is the trigger · wait words logged · cancels at 3:00 PM if it never trades 370"
    assert a["buttons"] == ["send_now", "cancel"] and a["send_ok"] is False
    assert {x["leg"] for x in a["kite"]["legs"]} >= {"entry", "stop"}             # what the shared confirms read
    assert d["card"]["pnl"] == 1400.0 + 480.0 and d["card"]["line"] == "DRY · 1 open · 1 armed · 2 of 4 entries"      # T1 600 watched: sold at market 610
    assert [(t["id"], t["text"], t["c"]) for t in d["today"]] == [(h1, "closed +₹1,400 · T1", "ok"), (h2, "armed · resting", "w"), (h3, "DRY · open", "ok")]
    assert d["checks"]["clear"] == [h3, h2, h1] and d["checks"]["items"] == [] and d["checks"]["at_label"] == "2:10 PM"
    assert d["record"]["hints"] == 3 and d["record"]["t1_first"] == 1 and d["record"]["day_check"] is None and d["record"]["day_check_at"] == "3:35 PM"
    assert d["channel30"]["hints"] == 3
    # a red order-check item names the hint and the hand, on Home and in Needs a look
    h = bk.load(h3)
    h["kite"]["check"] = {"ok": False, "red": 1, "at": t19("14:11").isoformat(), "items": [{"level": "red", "text": "no stop resting", "hand": "place the SL on Kite now"}]}
    ko.save_kite(h)
    d = th.home_tips(c, t19("14:12"))
    assert d["checks"]["items"] == [{"id": h3, "level": "red", "text": "no stop resting", "hand": "place the SL on Kite now"}]
    assert h3 not in d["checks"]["clear"]
    assert ("warn", f"⚡ Kite #{h3} · no stop resting → place the SL on Kite now") in kc.attention_items(t19("14:12"))
    # the armed entry cancelled: no entry · cancelled <time>
    ko.cancel(c, s, h2, "entry", now=t19("14:30"))
    d = th.home_tips(c, t19("14:31"))
    assert [r["id"] for r in d["rows"]] == [h3]
    assert next(t for t in d["today"] if t["id"] == h2)["text"] == "no entry · cancelled 2:30 PM"
    # after the day check the record line carries its summary
    kc.day_check(c, t19("15:35"), notify=False)
    assert th.home_tips(c, t19("15:40"))["record"]["day_check"]["summary"].startswith("🔎 Kite day · ")


def test_next_up_times_before_and_after_the_window():
    c, _, _ = auto_setup()
    lab = lambda now: [x["label"] for x in th.next_times(c, now)]       # noqa: E731
    assert lab(t19("10:00")) == ["3:00 PM", "3:14 PM", "3:35 PM"]
    assert [x["key"] for x in th.next_times(c, t19("10:00"))] == ["tips_window", "tips_time_stop", "tips_day_check"]
    assert lab(t19("15:10")) == ["3:14 PM", "3:35 PM"]
    assert lab(t19("15:40")) == []
    assert lab(at("10:00", date(2026, 11, 21))) == []                   # a Saturday
    c.tips.kite_mode = "off"
    assert lab(t19("10:00")) == []                                      # only while Kite mode is not off


def test_every_today_outcome():
    def h(state="none", **kw):
        k = {"state": state, **{x: kw.pop(x) for x in list(kw) if x in ("realised", "reason", "log", "auto", "dry")}}
        return {"id": 1, "status": kw.pop("status", "new"), "taken": kw.pop("taken", False), "kite": k}
    assert th.outcome(h("entered")) == ("open", "ok")
    assert th.outcome(h("entered", dry=True)) == ("DRY · open", "ok")
    assert th.outcome(h("armed")) == ("armed · resting", "w")
    assert th.outcome(h("armed_watch")) == ("armed", "w") and th.outcome(h("armed_wait")) == ("armed", "w")
    assert th.outcome(h("closed", realised=2480.0, reason="target 1")) == ("closed +₹2,480 · T1", "ok")
    assert th.outcome(h("closed", realised=-900.0, reason="stop")) == ("closed −₹900 · stop", "bad")
    assert th.outcome(h("closed", realised=10.0, reason="time stop 3:20 PM"))[0].endswith("· exit")
    assert th.outcome(h(reason="window closed", log=[{"t": "2026-11-19T14:30:00+05:30", "text": "Kite: the unfilled entry cancelled · window closed"}])) \
        == ("no entry · cancelled 2:30 PM", "")
    assert th.outcome(h(reason="entry rejected")) == ("no entry · entry rejected", "")
    assert th.outcome(h(auto={"why": "2nd open"})) == ("skipped · 2nd open", "a")
    assert th.outcome(h(taken=True, status="active")) == ("paper only", "")
    assert th.outcome(h(status="waiting")) == ("waiting", "")
    assert th.outcome(h(status="skipped")) == ("skipped", "a")
    assert th.outcome(h(status="expired")) == ("no entry", "")


def test_mode_off_payload(pushed):  # noqa: F811
    c, w, s, *_ = _day()
    c.tips.kite_mode = "off"
    d = th.home_tips(c, t19("14:12"))
    assert d["mode"] == "off" and d["rows"] == [] and d["next"] == [] and not d["live"]
    assert d["card"]["pnl"] is None and d["card"]["line"] == "paper book only"
    assert d["checks"]["items"] == [] and d["checks"]["clear"] == []
    assert len(d["today"]) == 3 and d["record"]["hints"] == 3            # the record and the calls stay readable


def test_route_and_stream_topic(api):                                   # noqa: F811
    from backend.api import routes
    r = api.get("/api/home/tips")
    assert r.status_code == 200 and set(r.json()) == KEYS and r.json()["mode"] == "off" and r.json()["rows"] == []
    assert "home_tips" in routes._stream_topics()
    src = (ROOT / "backend/tips/home.py").read_text(encoding="utf-8")
    assert ".quote(" not in src and "client" not in src.split('"""', 2)[2]     # no quote call, no Kite call


def test_home_markup_is_phone_safe_and_shares_the_tips_confirms():
    home = (ROOT / "frontend/src/components/HomePage.jsx").read_text(encoding="utf-8")
    blk = (ROOT / "frontend/src/components/HomeTips.jsx").read_text(encoding="utf-8")
    tips = (ROOT / "frontend/src/components/TipsPage.jsx").read_text(encoding="utf-8")
    shared = (ROOT / "frontend/src/components/tipsKite.js").read_text(encoding="utf-8")
    assert "TipsKiteBlock" in home and "TipsKiteCards" in home and "ht?.next" in home
    assert "<OvernightPanel />" in home and 'title="Open positions"' in home and 'title="Needs a look"' in home     # nothing removed
    assert "useLiveOr('home_tips', api.getHomeTips" in blk and "5000 : 30000" in blk
    for s in ("💡 Tips · Kite", "Open Tips", "Trail SL…", "Exit now…", "Send now…", "kill switch", "channel 30 d", "order check · all clear on"):
        assert s in blk, s
    for f in ("modeConfirm", "killConfirm", "kiteTexts"):
        assert "from './tipsKite'" in tips and f in tips and f in blk and (f"export function {f}" in shared or f"export const {f}" in shared), f
    assert "Real money on your Kite account" in shared and "Turn the Kite kill switch ON?" in shared and "Real money" not in blk
    css = (ROOT / "frontend/src/redesign.css").read_text(encoding="utf-8")
    assert ".rd .hk-row { display: grid; grid-template-columns: minmax(0, 1fr) auto" in css
    assert re.search(r"@media \(max-width: 700px\)[^@]*\.rd \.hk-row \{ grid-template-columns: minmax\(0, 1fr\)", css)
    assert re.search(r"@media \(max-width: 700px\)[^@]*\.rd \.hk-row \.a \{ grid-column: 1; grid-row: 3; flex-wrap: wrap; \}", css)
    assert ".rd .hk-today { display: flex; gap: 6px; flex-wrap: wrap" in css and ".rd .hk-strip { display: flex; align-items: center; gap: 10px; flex-wrap: wrap" in css
