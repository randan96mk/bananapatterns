"""🧩 More entries (paper), all three on by default: the same-bias re-entry after a stop (once per bias, a close back over
the range and VWAP, the chain, the daily loss guard, max_trades_day 3), re-entries until reentry_until (first entries
keep window_end), the afternoon pair at 13:30 on a quiet day (flat only, range <= 0.6%, inside the band, once a day, a
call spread above + a put spread below, exits without VWAP, one trade on the days row and the day summary), the
switches off = the old flow, the API / settings keys, the messages and the as-if replay of the pair's rows."""

from dataclasses import replace

import pytest

from backend.alerts import daysum
from backend.alerts import messages as msg
from backend.config import AppConfig
from backend.database.models import get_db
from backend.options import asif, book
from tests.test_options_asif import EXPIRY as AS_EXPIRY
from tests.test_options_asif import TODAY as AS_TODAY
from tests.test_options_asif import add_trade, net_of, seed_snaps
from tests.test_options_book import DAY, OLD_FLOW, World, at, cfg_for, cfg_new, path, run_day, tl

VWAP_STOP_THEN_BACK = {"09:30": 25050, "09:45": 25150, "09:51": 25080, "10:00": 25300}      # 9:55 VWAP stop, 10:05 back over
FLAT = {"09:30": 25050}                                                                        # no bias; day range 0.4%


@pytest.fixture
def api(fake_kite, make_client, monkeypatch):
    """The app (as tests/test_options_api.py's fixture): no scheduler, no Telegram, no Kite login."""
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("KITE_API_KEY", "key123")
    monkeypatch.setenv("KITE_API_SECRET", "sec")
    from fastapi.testclient import TestClient

    from backend import main
    client = make_client(fake_kite, history_years=1)
    client.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc, ic: client)
    with TestClient(main.app) as tc:
        yield tc


@pytest.fixture(autouse=True)
def prev():
    from tests.test_options_book import seed_prev_day
    seed_prev_day()


def rows():
    return sorted(book.trades(), key=lambda t: t["id"])


# --- 1 · the same bias again after a stop -----------------------------------------------------------------------------

def test_defaults_all_three_modes_on_and_three_trades():
    c = AppConfig().options
    assert (c.reentry_same_bias, c.reentry_late, c.reentry_until, c.afternoon_sell, c.afternoon_time, c.afternoon_max_range_pct, c.max_trades_day) == \
        (True, True, "13:00", True, "13:30", 0.6, 3)
    assert book.entries_text(c) == "entries: re-entry same bias · until 1:00 PM · afternoon sell 1:30 PM"
    assert book.entries_text(cfg_for().options) == "entries: first + one re-entry on a flipped bias"
    assert book.entries_text(replace(c, reentry_same_bias=False, afternoon_sell=False)) == "entries: re-entry until 1:00 PM"


def test_same_bias_reentry_after_a_vwap_stop():
    st = run_day(cfg_new(), World(path(VWAP_STOP_THEN_BACK)), end="10:10")
    d = st["day"]
    r = rows()
    assert r[0]["why"] == "vwap stop" and r[0]["kind"] == "first" and d["trades"] == 2 and d["kinds"] == ["first", "same"]
    assert d["biases"] == ["BULLISH", "BULLISH"] and d["same_used"] == ["BULLISH"] and st["open"]["kind"] == "same" and st["open"]["opened_t"] == "10:05 AM"
    line = next(x for x in tl(st) if x.startswith("re-entry · same bias"))
    assert "after the VWAP stop at 9:55 AM" in line and "back over the range high 25,100" in line and "VWAP" in line and "sold " in line
    ent = [a for a in st["alerts"] if a["kind"] == "entry"]
    assert len(ent) == 2 and ent[1]["data"]["kind"] == "same" and ent[1]["data"]["max_trades"] == 3 and "re-entry · same bias" in ent[1]["text"]
    tg = msg.options_alert(ent[1])
    assert "re-entry (2 of 3) · same bias · after the VWAP stop at 9:55 AM" in tg
    chk = next(x for x in d["checks"] if x["t"] == "10:05")
    assert chk["same"] is True and chk["prev"] == 1


def test_same_bias_only_once_per_bias():
    w = World(path({**VWAP_STOP_THEN_BACK, "10:11": 25080, "10:20": 25400}))       # the re-entry stops at 10:11; 10:25 is back over again
    st = run_day(cfg_new(daily_loss_pct=5.0), w, end="10:40")
    d = st["day"]
    assert [t["kind"] for t in rows()] == ["first", "same"] and [t["why"] for t in rows()] == ["vwap stop", "premium stop"]
    assert d["trades"] == 2 and st["open"] is None and any("no entry · same-bias re-entry used" in x for x in tl(st))


def test_same_bias_needs_the_close_back_over_range_and_vwap():
    st = run_day(cfg_new(), World(path({**VWAP_STOP_THEN_BACK, "10:00": 25105})), end="10:30")     # over the range high, under the VWAP
    assert st["day"]["trades"] == 1 and st["open"] is None and not any(x.startswith("re-entry") for x in tl(st))
    c = cfg_new().options
    d = {"biases": ["BULLISH"], "closed": [{"why": "vwap stop", "hm": "09:55", "kind": "first"}], "same_used": []}
    ok = {"checks": {"range": 1, "vwap": 1, "prev": 0}}
    assert book._same_bias_candidate(d, c, ok, "10:05") == "BULLISH"
    assert book._same_bias_candidate(d, c, {"checks": {"range": 1, "vwap": -1, "prev": 1}}, "10:05") is None        # under VWAP
    assert book._same_bias_candidate(d, c, {"checks": {"range": 0, "vwap": 1, "prev": 1}}, "10:05") is None         # inside the range
    assert book._same_bias_candidate(d, c, ok, "09:55") is None                                                    # not a later close
    assert book._same_bias_candidate({**d, "closed": [{"why": "target", "hm": "09:55"}]}, c, ok, "10:05") is None   # only after a stop
    assert book._same_bias_candidate({**d, "same_used": ["BULLISH"]}, c, ok, "10:05") is None                      # once per bias
    bear = {"biases": ["BEARISH"], "closed": [{"why": "premium stop", "hm": "10:00"}], "same_used": ["BULLISH"]}
    assert book._same_bias_candidate(bear, c, {"checks": {"range": -1, "vwap": -1, "prev": 0}}, "10:10") == "BEARISH"
    assert book._same_bias_candidate(d, replace(c, reentry_same_bias=False), ok, "10:05") is None


def test_same_bias_blocked_by_the_daily_loss_guard():
    st = run_day(cfg_new(daily_loss_pct=0.5), World(path(VWAP_STOP_THEN_BACK)), end="10:20")      # Rs 1,500: the first stop uses it up
    assert st["day"]["trades"] == 1 and rows()[0]["net"] < -1500
    assert any("BULLISH · no entry · daily loss limit" in x for x in tl(st)) and any(a["kind"] == "skipped" for a in st["alerts"])


def test_same_bias_counts_toward_max_trades_day():
    st = run_day(cfg_new(max_trades_day=1), World(path(VWAP_STOP_THEN_BACK)), end="10:20")
    assert st["day"]["trades"] == 1 and st["day"]["status"] == "done" and not any(x.startswith("re-entry") for x in tl(st))
    book.reset()
    with get_db() as conn:
        conn.execute("DELETE FROM options_trades")
    st = run_day(cfg_new(max_trades_day=2), World(path(VWAP_STOP_THEN_BACK)), end="10:10")
    assert st["day"]["trades"] == 2 and book.summary(cfg_new(max_trades_day=2), at("10:10"))["guards"]["trades"] == {"used": 2, "max": 2}


# --- 2 · re-entries until reentry_until -------------------------------------------------------------------------------

def test_reentry_until_gates_the_late_window_in_steps():
    cfg = cfg_new()
    st = book._new(cfg, DAY.isoformat())
    st["day"] = book._new_day(st, DAY.isoformat())
    d = st["day"]
    d["done"] = ["contracts", "range"]
    d["status"] = "no_trade"
    assert "bias" not in book.steps(at("11:30"), cfg, st)                         # no first entry after window_end
    d.update({"trades": 1, "status": "closed", "biases": ["BULLISH"]})
    assert "bias" in book.steps(at("12:30"), cfg, st) and "bias" in book.steps(at("13:00"), cfg, st)
    assert "bias" not in book.steps(at("13:05"), cfg, st)
    assert "bias" not in book.steps(at("12:30"), cfg_new(reentry_late=False), st)  # the switch off: window_end as before
    d["status"] = "open"
    assert "bias" not in book.steps(at("12:30"), cfg, st)                          # re-entries only once flat
    d["status"] = "closed"
    assert book._guard_reasons(st, cfg, at("13:05"), "BEARISH")[-1:] == ["re-entries end at 1:00 PM"]
    d["trades"] = 0
    assert "first entries end at 11:00 AM" in book._guard_reasons(st, cfg, at("11:30"), "BULLISH")


def test_reentry_at_12_30_allowed_then_the_day_is_done_at_13_00():
    st = run_day(cfg_new(), World(path({"09:30": 25050, "09:45": 25150, "09:51": 25080, "12:25": 25300})), end="12:35")
    d = st["day"]
    assert d["trades"] == 2 and st["open"]["opened_t"] == "12:30 PM" and st["open"]["late"] is True
    assert any("after 11:00 AM (re-entries to 1:00 PM)" in x for x in tl(st))
    book.reset()
    with get_db() as conn:
        conn.execute("DELETE FROM options_trades")
    st = run_day(cfg_new(), World(path({"09:30": 25050, "09:45": 25150, "09:51": 25080, "13:01": 25300})), end="13:10")
    d = st["day"]
    assert d["trades"] == 1 and st["open"] is None and d["status"] == "done" and d["checked"][-1] == "13:00"
    s = book.summary(cfg_new(), at("12:00"))
    assert s["guards"]["window"]["reentry_until"] == "1:00 PM"


def test_first_entry_at_11_30_still_refused():
    st = run_day(cfg_new(), World(path({"09:30": 25050, "11:25": 25300})), end="12:00")
    d = st["day"]
    assert d["trades"] == 0 and d["status"] == "no_trade" and book.trades() == [] and all(c <= "11:00" for c in d["checked"])


def test_closed_day_status_and_the_hint_follow_reentry_until():
    st = run_day(cfg_new(), World(path({"09:30": 25050, "09:45": 25150, "09:51": 25080})), end="11:30")
    assert st["day"]["status"] == "closed"                                        # old rule: done at 11:01; now open for re-entry
    h = book.summary(cfg_new(), at("11:30"))["today"]["hint"]
    assert h.startswith("re-entry possible until 1:00 PM · bullish again on a close back over the range and VWAP · or a bearish flip")
    assert "afternoon sell looks at 1:30 PM" in h
    st = run_day(cfg_new(), World(path({"09:30": 25050, "09:45": 25150, "09:51": 25080})), start="11:31", end="13:02")
    assert st["day"]["status"] == "done"


# --- 3 · the afternoon pair -------------------------------------------------------------------------------------------

def test_afternoon_pair_on_a_quiet_day():
    cfg = cfg_new()
    w = World(path(FLAT))
    st = run_day(cfg, w, end="13:35")
    d, o = st["day"], st["open"]
    assert d["status"] == "open" and d["trades"] == 1 and d["kinds"] == ["afternoon"] and o["kind"] == "afternoon" and o["opened_t"] == "1:30 PM"
    ce, pe = o["sides"]
    assert ce["sold"]["type"] == "CE" and pe["sold"]["type"] == "PE" and ce["sold"]["strike"] >= 25050 * 1.01 and pe["sold"]["strike"] <= 25050 * 0.99
    assert ce["hedge"]["strike"] - ce["sold"]["strike"] in (200, 250) and pe["sold"]["strike"] - pe["hedge"]["strike"] in (200, 250)
    assert ce["lots"] == pe["lots"] == 1 and ce["vwap_exit"] is False and ce["pair"] == pe["pair"] == f"{DAY.isoformat()}|1"
    assert ce["exits"] == "exits: premium 2× · time 3:10 PM" and d["afternoon"]["ok"] and d["afternoon"]["range_pct"] == 0.4
    a = next(x for x in st["alerts"] if x["kind"] == "afternoon")
    tg = msg.options_alert(a)
    assert tg.startswith("🧩 <b>Options · paper · afternoon sell · 1:30 PM</b> · 25,350 CE / 24,750 PE · credit ₹") and "range 0.4% · exits: premium 2× · time 3:10 PM" in tg
    assert "1 lot each side" in tg and "no VWAP exit" in tg
    v = book.summary(cfg, at("13:35"))
    assert v["open"]["kind"] == "afternoon" and len(v["open"]["legs"]) == 4 and v["open"]["footer"].startswith("afternoon pair 25,350 CE / 24,750 PE")
    assert v["margin"]["est"] == 104000.0 and st["cash"] > 300000                  # both credits are in the cash


def test_afternoon_pair_exits_and_the_days_row():
    cfg = cfg_new()
    st = run_day(cfg, World(path(FLAT)))
    r = rows()
    assert [t["kind"] for t in r] == ["afternoon", "afternoon"] and {t["side"] for t in r} == {"CE", "PE"} and r[0]["pair"] == r[1]["pair"]
    assert all(t["why"] == "time exit" and t["out"] == "3:10 PM" for t in r) and st["open"] is None and st["day"]["status"] == "done"
    row = book.days()[0]
    assert row["trades"] == 1 and row["rows"] == 2 and row["kinds"] == ["afternoon"] and row["strikes"] == "25,350 CE / 24,750 PE"
    assert row["result"] == "afternoon CE time exit / PE time exit" and row["pnl"] == pytest.approx(r[0]["net"] + r[1]["net"], abs=0.02)
    assert row["entries"] == "entries: re-entry same bias · until 1:00 PM · afternoon sell 1:30 PM"
    part = daysum.options_part(cfg, DAY.isoformat())
    assert part["trades"] == 1 and part["afternoon"] is True
    line = msg.day_summary({"options": part}, DAY.isoformat())
    assert "1 trade (incl. the afternoon pair)" in line and "afternoon CE time exit / PE time exit" in line
    ex = [a for a in st["alerts"] if a["kind"] == "exit"]
    assert len(ex) == 2 and msg.options_alert(ex[0]).startswith("⏱ <b>Options · paper · afternoon CE · time exit</b>")


def test_afternoon_sides_exit_on_their_own_and_never_on_vwap():
    st = run_day(cfg_new(), World(path({**FLAT, "13:40": 25000})), end="14:30")     # under the futures VWAP: a bullish spread would VWAP-stop
    assert st["open"] is not None and len(st["open"]["sides"]) == 2 and book.trades() == []
    book.reset()
    st = run_day(cfg_new(), World(path({**FLAT, "13:45": 25400})))
    r = rows()
    assert {(t["side"], t["why"]) for t in r} == {("CE", "premium stop"), ("PE", "target")} and not any(t["why"] == "vwap stop" for t in r)
    assert any(x.startswith("afternoon CE · premium stop") for x in tl(st)) and book.days()[0]["result"] == "afternoon CE premium stop / PE target"


def test_afternoon_only_when_flat_quiet_and_inside_the_band():
    st = run_day(cfg_new(), World(path({"09:30": 25050, "09:45": 25150}), basis=0.0), end="13:40")          # a spread open at 1:30 PM
    assert st["day"]["trades"] == 1 and any(x == "afternoon sell · no entry · a spread is open" for x in tl(st))
    book.reset()
    st = run_day(cfg_new(), World(path({"09:30": 25050, "09:45": 24910, "10:00": 25050})), end="13:40")      # a 0.76% day
    assert st["day"]["trades"] == 0 and any("afternoon sell · no entry · day range 0.76% > 0.6%" in x for x in tl(st))
    book.reset()
    st = run_day(cfg_new(afternoon_max_range_pct=1.0), World(path({"09:30": 25050, "11:05": 25190})), end="13:40")   # quiet but over the band
    assert st["day"]["trades"] == 0 and any("spot 25,190 outside 24,938–25,163" in x for x in tl(st))
    book.reset()
    st = run_day(cfg_new(event_days=DAY.isoformat()), World(path(FLAT)), end="13:40")
    assert st["day"]["trades"] == 0 and any("afternoon sell · no entry · event day" in x for x in tl(st))


def test_afternoon_step_once_a_day_at_or_after_its_time():
    cfg = cfg_new()
    st = book._new(cfg, DAY.isoformat())
    st["day"] = book._new_day(st, DAY.isoformat())
    st["day"]["done"] = ["contracts", "range"]
    st["day"]["status"] = "no_trade"
    assert "afternoon" not in book.steps(at("13:29"), cfg, st) and "afternoon" in book.steps(at("13:30"), cfg, st)
    assert "afternoon" in book.steps(at("14:10"), cfg, st) and "afternoon" not in book.steps(at("15:10"), cfg, st)
    assert "afternoon" not in book.steps(at("13:30"), cfg_new(afternoon_sell=False), st)
    st["day"]["done"].append("afternoon")
    assert "afternoon" not in book.steps(at("13:31"), cfg, st)


def test_afternoon_respects_the_daily_loss_guard_and_max_trades():
    st = run_day(cfg_new(daily_loss_pct=0.5), World(path({"09:30": 25050, "09:45": 25150, "09:51": 25080, "10:00": 25040})), end="13:40")
    assert st["day"]["trades"] == 1 and any("afternoon sell · no entry · daily loss limit" in x for x in tl(st))
    book.reset()
    st = run_day(cfg_new(max_trades_day=1), World(path({"09:30": 25050, "09:45": 25150, "09:51": 25080, "10:00": 25040})), end="13:40")
    assert st["day"]["trades"] == 1 and any("afternoon sell · no entry · 1 trades done" in x for x in tl(st))


# --- switches off = the old flow -----------------------------------------------------------------------------------

def test_switches_off_is_the_old_flow():
    off = cfg_new(**OLD_FLOW)
    st = run_day(off, World(path(VWAP_STOP_THEN_BACK)), end="11:30")
    assert st["day"]["trades"] == 1 and any("re-entry only on a flipped bias" in x for x in tl(st)) and st["day"]["status"] == "done"
    book.reset()
    st = run_day(off, World(path(FLAT)))
    assert st["day"]["status"] == "no_trade" and "afternoon" not in st["day"]["done"] and [a["kind"] for a in st["alerts"]] == ["no_trade", "summary"]
    assert not any("afternoon" in x for x in tl(st)) and book.summary(off, at("15:40"))["today"]["hint"] is None


# --- API / settings, messages, the as-if replay -------------------------------------------------------------------------

def test_switches_and_settings_keys(api):
    tc = api
    r = tc.put("/api/options/settings", json={"reentry_same_bias": False, "reentry_late": False, "afternoon_sell": False}).json()
    assert (r["reentry_same_bias"], r["reentry_late"], r["afternoon_sell"]) == (False, False, False)
    d = tc.get("/api/options").json()
    assert d["entries"] == "entries: first + one re-entry on a flipped bias" and d["guards"]["entries"]["late"] is False
    assert tc.put("/api/options/settings", json={"afternoon_sell": True}).json()["afternoon_sell"] is True
    out = tc.put("/api/config/options", json={"reentry_until": "12:30", "afternoon_time": "13:45", "afternoon_max_range_pct": 0.5, "reentry_late": True}).json()
    assert (out["reentry_until"], out["afternoon_time"], out["afternoon_max_range_pct"]) == ("12:30", "13:45", 0.5)
    assert tc.put("/api/config/options", json={"reentry_until": "25:00"}).status_code == 422
    assert tc.get("/api/config/options").json()["reentry_until"] == "12:30"
    d = tc.get("/api/options").json()
    assert d["entries"] == "entries: re-entry until 12:30 PM · afternoon sell 1:45 PM" and "re-entries until 12:30 PM" in d["rules"][8]
    assert any(x["text"].startswith("afternoon sell") and x["t"] == "1:45 PM" for x in d["today"]["schedule"])


def test_messages_reentry_and_afternoon_lines():
    from tests.test_options_alerts import MOCK_OPEN
    flip = msg.options_bias({**MOCK_OPEN, "n": 2, "kind": "flip", "max_trades": 3})
    assert "<i>re-entry (2 of 3) · the bias flipped</i>" in flip
    same = msg.options_bias({**MOCK_OPEN, "n": 3, "kind": "same", "max_trades": 3, "entry_why": "after the premium stop at 10:02 AM: close 26,140 back over",
                             "late": True, "window": {"reentry_until": "13:00"}})
    assert "re-entry (3 of 3) · same bias · after the premium stop at 10:02 AM" in same and "re-entries to 1:00 PM" in same
    nt = msg.options_no_trade({"range": {"low": 25000, "high": 25100}, "window_end": "11:00", "next": "2026-11-18", "afternoon": "13:30"})
    assert "afternoon sell looks at 1:30 PM" in nt
    assert book.exits_text_afternoon(100, "15:10") == "exits: premium 2× · time 3:10 PM"
    for t in (flip, same, nt):
        assert "Zerodha" not in t and "Tradetron" not in t


def test_asif_replays_the_afternoon_rows_with_vwap_off():
    seed_snaps(AS_TODAY.replace(day=10).isoformat(), lambda t: 36.0 if t < "11:00" else 14.0, lambda t: 9.0 if t < "11:00" else 3.0)
    add_trade(why="vwap stop", closed="10:32:05", sold_exit=45.0, hedge_exit=8.0, net=-1500.0, kind="afternoon", pair="2026-11-10|1", side="PE", bias="NEUTRAL")
    rep = asif.report(AppConfig(), 60, today=AS_TODAY)
    pt = rep["per_trade"][0]
    assert pt["kind"] == "afternoon" and pt["pair"] == "2026-11-10|1" and AS_EXPIRY
    assert pt["by_rule"]["x2_vwap"]["why"] == "target" and pt["by_rule"]["asis"]["why"] == "target" and pt["by_rule"]["x2_vwap"]["net"] == net_of(14.0, 3.0)


def test_saved_two_trades_a_day_is_raised_to_three_once():
    from backend.config import load_config, save_config
    old = AppConfig()
    old.options.max_trades_day = 2                                     # a server that saved the old default
    save_config(old)
    assert load_config(AppConfig()).options.max_trades_day == 3
    save_config(old)                                                   # set back to 2 by hand afterwards: kept
    assert load_config(AppConfig()).options.max_trades_day == 2
