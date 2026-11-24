"""✨ Home › Today, the phase-aware tiles (backend/ai/today_tiles.py): the phase for every time of day (a holiday
included), every tile's rows in each phase from a seeded world, the last-session fallback, the fresh-install strings,
the will-send / held tags, the slippage tag, [AI] lines only from stored rows, the markup markers, and that building the
payload never touches an order path (no send, no skip, no model, quote or broker call)."""

import json
from pathlib import Path

import pytest

from backend.ai import today
from backend.ai import today_tiles as tt
from backend.database.models import get_db
from backend.engine import real
from tests import today_world as w
from tests.test_ai_infra import no_brokers

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def calendar(monkeypatch):
    from backend.ingest import holidays as hol
    monkeypatch.setattr(hol, "is_trading_day", w.trading)


@pytest.fixture(autouse=True)
def no_order_path(monkeypatch):
    """Every way out to a broker or a model raises: the panel must never reach one."""
    def boom(*a, **k):
        raise AssertionError("the Today panel touched an order / model path")
    from backend.ai import client
    from backend.ai import queue as q
    from backend.broker import tradetron
    from backend.tips import kite_orders as ko
    monkeypatch.setattr(tradetron, "send", boom)
    monkeypatch.setattr(real, "send_orders", boom)
    monkeypatch.setattr(real, "watch_stops", boom)
    monkeypatch.setattr(real, "skip_order", boom)
    monkeypatch.setattr(q, "enqueue", boom)
    monkeypatch.setattr(client, "generate", boom)
    for name in ("place", "cancel", "modify", "exit_now", "send_now"):
        if hasattr(ko, name):
            monkeypatch.setattr(ko, name, boom)


def panel(hhmm: str, day: str, cfg=None) -> dict:
    return today.today_card(cfg or w.cfg(), w.at(day, hhmm))["panel"]


def texts(t: dict) -> list[str]:
    return [(r.get("b") or "") + r.get("n", "") if r.get("kind") != "step" else r["t"] for r in t["rows"]]


def tile(p: dict, key: str) -> dict:
    return next(t for t in p["tiles"] if t["key"] == key)


# --- the phase ------------------------------------------------------------------------------------------------------------

def test_the_phase_for_each_time_of_day_and_a_holiday():
    c = w.cfg()
    p = lambda day, hhmm: tt.panel_phase(w.at(day, hhmm), c)            # noqa: E731
    assert [p(w.THU, x) for x in ("00:10", "05:59", "06:00", "08:59", "09:15", "09:16", "11:42", "15:39", "15:40", "19:57", "23:59")] == \
        ["evening", "evening", "premarket", "premarket", "premarket", "market", "market", "market", "evening", "evening", "evening"]
    assert p("2026-11-24", "10:00") == "evening" and p("2026-11-28", "11:00") == "evening"      # a holiday, a Saturday
    hol = panel("10:00", "2026-11-24")
    assert hol["phase"] == "evening" and hol["chip"] == "holiday · next session Wed"
    assert panel("08:40", w.THU)["chip"] == "pre-market" and panel("11:42", w.THU)["chip"] == "market hours · 11:42 AM"
    assert [panel(x, w.THU)["step"] for x in ("08:40", "09:05", "11:42", "15:35", "16:00", "19:00")] == [0, 1, 1, 2, 3, 4]


# --- evening ----------------------------------------------------------------------------------------------------------------

def test_evening_tiles_from_the_seeded_world():
    w.seed_real()
    w.seed_night()
    w.held_reads()
    p = panel("19:57", w.WED)
    assert p["phase"] == "evening" and p["one"] == "tonight: notes, why, suggestions, ratings · tomorrow: 3 orders queued for 9:16 AM"
    t1 = tile(p, "tomorrow")
    assert t1["title"] == "Tomorrow · 9:16 AM" and t1["meta"].startswith("4 queued · ") and not t1["last"]
    abc = next(r for r in t1["rows"] if r.get("b") == "ABC")
    assert abc["n"] == " · buy · E+" and abc["v"][0]["t"] == "₹30,096 · 24 sh" and abc["s"].startswith("stop 1,196")
    tips = next(r for r in t1["rows"] if r["n"] == "Tips channel")
    assert "caps 0 of 4 entries · 2 lots max · risk ₹6,000" in tips["s"]
    cm = next(r for r in t1["rows"] if r["n"] == "candle model · held names")
    assert cm["tag"] == {"t": "shadow", "c": "w"} and cm["s"] == "DEF stop-first 52 % · GHI up 66 % · read 7:05 PM"
    t2 = tile(p, "tonight")
    steps = [r for r in t2["rows"] if r.get("kind") == "step"]
    assert [(r["t"], r["st"], r["at"]) for r in steps] == [
        ("daily candles to Wed 25 Nov", "done", "6:36 PM"), ("evening scan · 581 names", "done", "6:36 PM"),
        ("notes · 2 filings (LTTS ×2)", "done", "7:27 PM"), ("why · the lines under today's exits", "skipped", "skipped"),
        ("ratings · 1 of 1 · suggestions 0", "done", "7:27 PM"), ("candle model scorecard read", "queued", "queued")]
    ai = [r for r in t2["rows"] if r.get("ai")]
    assert len(ai) == 2 and ai[0]["n"] == "LTTS: a board meeting date for the results"
    assert t2["meta"] == "6:30 PM → 7:27 PM · running"
    t3 = tile(p, "books")
    real_row = t3["rows"][0]
    assert real_row["b"] == "Real" and real_row["v"][0] == {"t": "−₹522", "c": "down"} and real_row["v"][1]["t"] == "₹3,01,435"
    assert real_row["s"] == "worst DEF −1.3 % · best MNO +0.9 % · 3 of 14 slots · 1 fill unconfirmed"
    assert t3["meta"] == "today"                                          # WED had sent orders
    assert p["footer"].endswith("nothing here acts on its own")


def test_evening_before_the_night_batch_says_when():
    w.seed_real()
    p = panel("16:10", w.WED)
    t2 = tile(p, "tonight")
    assert t2["rows"][0]["n"] == "tonight: notes, why, suggestions, ratings after 6:30 PM" and t2["meta"] == "from 6:30 PM · queued"
    assert all(r["st"] == "queued" for r in t2["rows"][1:])


# --- pre-market ----------------------------------------------------------------------------------------------------------

def test_premarket_tags_kite_row_watch_and_books():
    w.seed_real()
    w.seed_night()
    w.held_reads()
    w.seed_morning()
    p = panel("08:40", w.THU)
    assert p["phase"] == "premarket" and p["one"] == "9:16 AM: 3 orders will go · 1 held · Kite logged in · the brief is out"
    t1 = tile(p, "orders")
    tags = {r.get("b"): r.get("tag") for r in t1["rows"]}
    assert tags["ABC"] == {"t": "will send", "c": "ok"} and tags["GHI"] == {"t": "will send", "c": "ok"}
    assert tags["JKL"] == {"t": "held", "c": "w"} and next(r for r in t1["rows"] if r.get("b") == "JKL")["s"].startswith("skipped by you · results Friday")
    kite = t1["rows"][-1]
    assert kite["n"] == "Kite" and [x["t"] for x in kite["v"]] == ["logged in", "valid till 6:00 AM"]
    assert kite["s"] == "webhook token missing · orders confirm before sending" and "tok-test" not in json.dumps(p)
    t2 = tile(p, "watch")
    assert [r["n"] for r in t2["rows"] if r.get("ai")] == ["Two buys and one stop-exit go at 9:16; JKL is held for its results",
                                                           "DEF sits 3.1 % above its stop"]
    names = texts(t2)
    assert "India VIX" in names and "Held names · candle model" in names and "Filings overnight" in names and "Tips channel" in names
    assert not any("GIFT" in x or "SGX" in x or "futures" in x.lower() for x in json.dumps(p, ensure_ascii=False).split('"'))
    fil = next(r for r in t2["rows"] if r["n"] == "Filings overnight")
    assert fil["s"] == "LTTS · no held name"
    t3 = tile(p, "books")
    assert t3["title"] == "Books · yesterday close" and t3["meta"].startswith("open risk ₹")
    assert t3["rows"][0]["s"] == "3 held · 1 within 3 % of the stop"
    assert p["buttons"] == [{"label": "Confirm the 9:16 send…", "kind": "go", "arg": "portfolio/real"}]


def test_held_when_the_kill_switch_is_on():
    w.seed_real()
    real.set_killed(True)
    q = real.presend(w.cfg(), w.at(w.THU, "08:40"))
    assert {o["tag"] for o in q["orders"]} == {"held"} and q["orders"][0]["reason"] == "the kill switch is on"
    real.set_killed(False)
    st = real._state()
    st["cash"] = 31_000.0                                                  # GHI's exit adds 21,310: ABC fits, PQR does not
    real._save(st)
    tags = {o["symbol"]: (o["tag"], o["reason"]) for o in real.presend(w.cfg(), w.at(w.THU, "08:40"))["orders"]}
    assert tags["ABC"] == ("will send", None) and tags["PQR"] == ("held", "not enough cash")


def test_brief_not_out_yet():
    w.seed_real()
    p = panel("08:10", w.THU)
    assert tile(p, "watch")["rows"][0]["n"] == "brief at 9:00 AM" and p["one"].endswith("the brief at 9:00 AM")


# --- market hours ------------------------------------------------------------------------------------------------------------

def test_market_orders_slippage_watch_and_books():
    w.seed_real("market")
    w.seed_night()
    w.held_reads()
    w.seed_morning()
    p = panel("11:42", w.THU)
    assert p["phase"] == "market" and p["one"] == "orders done: 3 sent · 1 filled · 2 names near a level"
    t1 = tile(p, "orders")
    rows = {r.get("b"): r for r in t1["rows"]}
    assert rows["ABC"]["v"][0]["t"] == "sent 1,257.2" and rows["ABC"]["tag"] == {"t": "+0.3 %", "c": "ok"}
    assert rows["PQR"]["tag"] == {"t": "+0.6 %", "c": "w"}
    assert rows["GHI"]["v"][0]["t"] == "filled 2,131" and rows["GHI"]["tag"]["t"] == "done"
    assert rows["JKL"]["tag"] == {"t": "skipped", "c": "w"}
    assert t1["rows"][-1]["n"] == "next" and t1["rows"][-1]["s"] == "time stop 3:14 PM · day check 3:35 PM"
    t2 = tile(p, "watch")
    d = t2["rows"][0]
    assert d["b"] == "DEF" and d["v"][1] == {"t": "stop 805 is 2.9 % away", "c": "down"} and "stop-first 52 % (shadow)" in d["s"]
    assert t2["rows"][1]["b"] == "MNO" and t2["rows"][1]["v"][1]["c"] == "up"
    assert tile(p, "books")["title"] == "Books · live"
    assert p["buttons"] == [{"label": "Open positions", "kind": "go", "arg": "portfolio/portfolio"}, {"label": "Tips", "kind": "go", "arg": "research/tips"}]


def test_slippage_tag():
    assert tt.slip_pct("BUY", 1257.2, 1256.0) == 0.1 and tt.slip_tag(0.1) == ("+0.1 %", "ok")
    assert tt.slip_tag(0.0) == ("0 %", "ok") and tt.slip_tag(0.5) == ("+0.5 %", "ok") and tt.slip_tag(0.6) == ("+0.6 %", "w")
    assert tt.slip_pct("EXIT", 990.0, 1000.0) == 1.0 and tt.slip_tag(-0.7) == ("−0.7 %", "w")
    assert tt.slip_pct("BUY", None, 100.0) is None and tt.slip_tag(None) is None


def test_sparkline_and_all_books_from_the_minute_series(monkeypatch):
    from backend.engine import overview
    w.seed_real("market")
    curve = [(f"{w.THU}T09:21:00+05:30", -120.0), (f"{w.THU}T10:30:00+05:30", 900.0), (f"{w.THU}T11:38:00+05:30", 3900.0),
             (f"{w.THU}T11:41:00+05:30", 3690.0)]
    monkeypatch.setattr(overview, "_today_curve", curve)
    t3 = tile(panel("11:42", w.THU), "books")
    assert t3["rows"][1]["kind"] == "spark" and t3["rows"][1]["pts"] == [-120.0, 900.0, 3900.0, 3690.0] and t3["rows"][1]["c"] == "up"
    assert t3["rows"][-1]["s"] == "high +₹3,900 at 11:38 AM · low −₹120 at 9:21 AM"


def test_tips_rows_from_the_home_rows(pushed):  # noqa: F811
    from tests.test_tips_home import _day
    from tests.test_tips_kite_auto import t19
    c, _, _, h1, h2, h3 = _day()
    c.tips.kite_mode = "dry"
    rows = tt._tips_live_rows(c, t19("14:12"))
    assert [r["b"] for r in rows] == [f"Tips #{h3}", f"Tips #{h2}"]
    assert rows[0]["v"][0] == {"t": "+₹480", "c": "up"} and rows[0]["_open"] and not rows[1]["_open"]
    dist = tt._tips_dist_rows(c, t19("14:12"))
    assert dist[0]["b"] == f"#{h3}" and dist[0]["v"][1]["t"] == "+15.2 % to go" and dist[0]["s"].startswith("stop 250 is −24.2 %")
    day = tt._tips_day(c, "2026-11-19")
    assert day["n"] == 2 and day["parts"][0] == f"#{h1} +1,400" and day["one"] is None


from tests.test_tips_kite_auto import pushed  # noqa: E402,F401


def test_tips_review_one_thing_only_from_a_stored_row(pushed):  # noqa: F811
    from backend.tips import ai4
    from tests.test_tips_home import _day
    c, _, _, h1, _, _ = _day()
    rid = ai4.insert("review", h1, "2026-11-19", {"text": "x"}, w.at("2026-11-19", "15:35"))
    assert tt._tips_day(c, "2026-11-19")["one"] is None                    # pending: no line
    ai4._finish(rid, {"lines": [{"label": "one thing", "text": "a 12:50 PM call, booked at T1"}]}, "m", 10, w.at("2026-11-19", "15:36"))
    assert tt._tips_day(c, "2026-11-19")["one"] == f"#{h1}: a 12:50 PM call, booked at T1"


# --- the fallbacks ----------------------------------------------------------------------------------------------------------

def test_last_session_fallback_greys_the_rows_with_the_date():
    w.seed_real("market")                                                 # THU's orders logged ...
    with get_db() as conn:
        conn.execute("DELETE FROM real_orders WHERE day = ?", (w.THU,))   # (test DB only) ... then none for THU
    p = panel("11:42", w.THU)
    t1 = tile(p, "orders")
    assert t1["last"] and t1["meta"] == "last session · Wed 25 Nov"
    assert [r["b"] for r in t1["rows"][:2]] == ["STU", "VWX"] and all(r.get("dim") for r in t1["rows"][:2])
    assert tt.EMPTY_ORDERS not in json.dumps(t1["rows"], ensure_ascii=False)
    # the evening with no plan queued: the last session's orders under "Tomorrow"
    st = real._state()
    st["orders"], st["as_of"] = [], w.WED
    real._save(st)
    t = tile(panel("19:57", w.WED), "tomorrow")
    assert t["last"] and t["meta"] == "last session · Wed 25 Nov" and t["rows"][0]["b"] == "STU"


def test_a_holiday_shows_the_last_sessions_night_greyed():
    w.seed_real()
    w.seed_night()
    p = panel("11:00", "2026-11-28")                                        # Saturday
    t2 = tile(p, "tonight")
    assert t2["last"] and t2["meta"] == "last session · Wed 25 Nov" and t2["rows"][0]["st"] == "done"
    assert tile(p, "books")["title"] == "Books · last session"


def test_fresh_install_keeps_the_empty_strings():
    for hhmm, day in (("19:57", w.WED), ("08:40", w.THU), ("11:42", w.THU), ("10:00", "2026-11-28")):
        p = panel(hhmm, day)
        orders = p["tiles"][0]
        assert not orders["last"] and orders["empty"] == tt.EMPTY_ORDERS
        assert not any(r.get("b") for r in orders["rows"])                  # no order row: only the standing rows
    m = panel("11:42", w.THU)
    assert tile(m, "watch")["rows"] == [] and tile(m, "watch")["empty"] == tt.EMPTY_WATCH
    assert tile(panel("10:00", "2026-11-28"), "tonight")["rows"] == []


# --- [AI] only from stored rows · no order path · the payload ---------------------------------------------------------------

def test_ai_lines_only_from_stored_rows():
    w.seed_real()
    w.seed_night()
    w.seed_morning()
    with get_db() as conn:
        stored = {r[0] for r in conn.execute("SELECT text FROM ai_notes")} | {"LTTS: " + r[0] for r in conn.execute("SELECT text FROM ai_notes")} | \
            {ln for r in conn.execute("SELECT data FROM ai_notes WHERE kind = 'brief'") for ln in json.loads(r[0])["lines"]}
        conn.execute("INSERT INTO ai_notes (created, day, kind, symbol, title, text, data, prompt_version) VALUES (?, ?, 'filing', 'XYZ', 't', "
                     "'no model wrote this', '{}', 'notes-v1')", (w.at(w.WED, "19:40").isoformat(), w.WED))
    for hhmm, day in (("19:57", w.WED), ("08:40", w.THU)):
        p = panel(hhmm, day)
        ai = [r["n"] for t in p["tiles"] for r in t["rows"] if r.get("ai")]
        assert ai and all(x in stored for x in ai) and not any("no model wrote this" in x for x in ai)


def test_nothing_touches_an_order_path():
    st = w.seed_real()
    w.seed_night()
    w.seed_morning()
    with get_db() as conn:
        before = (conn.execute("SELECT COUNT(*) FROM real_orders").fetchone()[0], conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM tips_orders").fetchone()[0])
    for hhmm, day in (("05:00", w.THU), ("08:40", w.THU), ("11:42", w.THU), ("19:57", w.WED), ("10:00", "2026-11-24")):
        c = today.today_card(w.cfg(), w.at(day, hhmm))
        assert c["panel"] is not None and no_brokers(json.dumps(c["panel"], ensure_ascii=False))
    with get_db() as conn:
        after = (conn.execute("SELECT COUNT(*) FROM real_orders").fetchone()[0], conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0],
                 conn.execute("SELECT COUNT(*) FROM tips_orders").fetchone()[0])
    assert before == after and real._state()["skips"] == st["skips"] and real._state()["orders"] == st["orders"]


def test_the_test_clock_is_only_honoured_without_the_scheduler(monkeypatch):
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    assert today.clock("2026-11-26T11:42:00").isoformat() == "2026-11-26T11:42:00+05:30"
    monkeypatch.setenv("BP_TODAY_AT", "2026-11-25T19:57:00")
    assert today.clock().isoformat() == "2026-11-25T19:57:00+05:30"
    monkeypatch.delenv("BP_NO_SCHEDULER")
    from datetime import datetime
    from backend.config import IST
    assert abs((today.clock("2026-11-26T11:42:00") - datetime.now(IST)).total_seconds()) < 60     # production: the real clock


def test_markup_markers():
    src = (ROOT / "frontend/src/components/TodayCard.jsx").read_text()
    css = (ROOT / "frontend/src/redesign.css").read_text()
    for marker in ("tt-tile", "data-tile={t.key}", "' last'", "tt-step", "tt-spark", "<AiTag />", "pn.chip", "pn?.one", "pn?.footer",
                   "Open the dry run", "tc-ribbon", "skipConfirmText"):
        assert marker in src, marker
    assert ".rd .tt-row { flex: none; height: 22px;" in css and ".rd .tt-row.tt2 { height: 40px; }" in css and "text-overflow: ellipsis" in css
    assert ".rd .tt-tile.last .tt-row" in css and "@media (max-width: 760px) { .rd .tt-tile { min-height: 0; } }" in css
