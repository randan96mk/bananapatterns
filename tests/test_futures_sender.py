"""📐 The futures deployment's webhook sender (step 4): the payload shapes (BUY / SELL with lots, EXIT without, M&M
sent as named, sig_action last), the kill body, the pacing on the shared Tradetron clock (10 s floor), 429 retries
then success, every other HTTP error raised once, a timeout → SendUnknown (the book logs `unknown · check` and never
re-sends), REJECTED_ALREADY_HELD → AlreadyHeld (logged "already held at the deployment", plan left unfilled), the
market-hours and no-token refusals, the token never in a logged / public payload, and `check()` in the summary."""

import json
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from backend.broker import tradetron as tt
from backend.config import IST
from backend.database.models import get_db
from backend.futures import book, sender
from backend.ingest import holidays as hol
from tests.test_futures_book import Kite, cut, world

TOKEN = "SECRET-FUT-TOKEN-123"
OPEN_AT = datetime(2026, 11, 12, 10, 0, tzinfo=IST)          # a Thursday, in hours


class _Reply:
    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text.encode()


def _opener(script):
    """A fake Tradetron: each call takes the next answer (an HTTP code to raise, an exception, or a reply text)."""
    calls = []

    def op(req, timeout=15):
        calls.append(req)
        ans = script[len(calls) - 1]
        if isinstance(ans, int):
            raise urllib.error.HTTPError(req.full_url, ans, "x", {"Retry-After": "3"} if ans == 429 else {}, None)
        if isinstance(ans, Exception):
            raise ans
        return _Reply(ans)
    return op, calls


class Log:
    def __init__(self):
        self.lines = []

    def info(self, fmt, *a):
        self.lines.append(fmt % a)

    warning = info


@pytest.fixture
def live(monkeypatch):
    """A token, the market open, no real sleeping, a recording logger; the shared pace clock reset."""
    monkeypatch.setenv(sender.ENV_TOKEN, TOKEN)
    monkeypatch.setattr(hol, "is_trading_day", lambda d, holidays=None: True)
    slept = []
    monkeypatch.setattr(tt.time, "sleep", lambda s: slept.append(s))
    tt._last_sent[0] = 0.0
    log = Log()
    monkeypatch.setattr(sender, "logger", log)
    return {"slept": slept, "log": log}


def _sent(calls):
    return [json.loads(c.data.decode()) for c in calls]


# --- payloads -------------------------------------------------------------------------------------------------

def test_buy_sell_exit_payloads_and_m_and_m(live):
    op, calls = _opener(["ok", "ok", "ok"])
    assert sender.send("RELIANCE", "BUY", 2, opener=op, now=OPEN_AT) == "ok"
    sender.send("M&M", "SELL", 1, opener=op, now=OPEN_AT)
    sender.send("M&M", "EXIT", 3, opener=op, now=OPEN_AT)                      # lots are ignored for EXIT
    bodies = _sent(calls)
    assert bodies[0] == {"auth-token": TOKEN, "key": "sig_symbol", "value": "RELIANCE", "key1": "sig_qty", "value1": "2",
                         "key2": "sig_action", "value2": "BUY"}
    assert bodies[1] == {"auth-token": TOKEN, "key": "sig_symbol", "value": "M&M", "key1": "sig_qty", "value1": "1",
                         "key2": "sig_action", "value2": "SELL"}
    assert bodies[2] == {"auth-token": TOKEN, "key": "sig_symbol", "value": "M&M", "key1": "sig_action", "value1": "EXIT"}
    assert list(bodies[0]) == ["auth-token", "key", "value", "key1", "value1", "key2", "value2"]            # pairs in order
    assert b'"value": "M&M"' in calls[1].data                                                                # sent as the NSE name
    req = calls[0]
    assert req.full_url == sender.URL and req.get_header("User-agent") == "tt-webhook/1.0"
    assert req.get_header("Content-type") == "application/json"
    # one signal per call, lowercase / padded input is normalised, bad actions and lots refused before anything goes out
    assert sender.body(" tcs ", "buy", "4", "t")["value"] == "TCS" and sender.body("tcs", "buy", "4", "t")["value1"] == "4"
    for bad in [("TCS", "HOLD", 1), ("TCS", "BUY", 0), ("TCS", "BUY", None), ("TCS", "SELL", "x"), ("", "BUY", 1)]:
        with pytest.raises(ValueError):
            sender.body(*bad)
    assert len(calls) == 3


def test_kill_switch_body_record_and_no_token(live, monkeypatch):
    sender.sent_kills.clear()
    op, calls = _opener(["killed"])
    assert sender.kill_switch("futures kill switch", opener=op) == "killed"
    assert _sent(calls) == [{"auth-token": TOKEN, "key": "kill_switch", "value": "1"}]
    assert sender.sent_kills[-1]["note"] == "futures kill switch" and sender.sent_kills[-1]["reply"] == "killed"
    assert TOKEN not in json.dumps(sender.sent_kills)
    monkeypatch.delenv(sender.ENV_TOKEN)
    with pytest.raises(RuntimeError, match="TRADETRON_FUTURES_TOKEN"):
        sender.kill_switch("x", opener=op)
    assert len(calls) == 1 and len(sender.sent_kills) == 1


def test_kill_switch_through_the_book(live, monkeypatch):
    cfg, data, i = world(monkeypatch, long_real=True)
    book.evening(cfg, cut(data, i), today=data["cal"][i])
    sender.sent_kills.clear()
    op, calls = _opener(["ok"])
    monkeypatch.setattr(urllib.request, "urlopen", op)
    out = book.set_killed(True, cfg)
    assert out["sent"] and out["reply"] == "ok" and _sent(calls)[0]["key"] == "kill_switch" and len(sender.sent_kills) == 1
    assert book.set_killed(True, cfg)["sent"] is False and len(calls) == 1                                  # once
    assert book.summary(cfg)["kills"][0]["note"] == "futures kill switch"


# --- pacing, retries, errors ----------------------------------------------------------------------------------

def test_webhooks_are_at_least_the_gap_apart(live, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(tt.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tt.time, "sleep", lambda s: (live["slept"].append(s), clock.__setitem__(0, clock[0] + s)))
    op, _ = _opener(["ok"] * 6)
    sender.send("A", "BUY", 1, opener=op, now=OPEN_AT)
    assert live["slept"] == []                                                                              # the first goes at once
    sender.send("B", "BUY", 1, opener=op, now=OPEN_AT)
    assert live["slept"] == [10.0]                                                                          # the default floor
    sender.send("C", "EXIT", opener=op, gap=3, now=OPEN_AT)
    assert live["slept"][-1] == 10.0                                                                        # a shorter gap is floored
    sender.send("D", "EXIT", opener=op, gap=20, now=OPEN_AT)
    assert live["slept"][-1] == 20.0                                                                        # real.send_gap_s raises it
    # the clock is the cash sender's: a cash webhook just before makes the futures one wait too
    monkeypatch.setenv("TRADETRON_TOKEN", "cash")
    tt.send("CASH", "BUY", 5, opener=op)
    sender.send("E", "EXIT", opener=op, now=OPEN_AT)
    assert live["slept"][-1] == 10.0


def test_429_is_retried_then_accepted_other_http_errors_raised_once(live):
    op, calls = _opener([429, '{"success":true}'])
    assert sender.send("GLENMARK", "BUY", 5, opener=op, now=OPEN_AT) == '{"success":true}'
    assert len(calls) == 2 and sender.last_retries == 1 and 3.0 in live["slept"]                           # Retry-After 3 s > 2 s
    op, calls = _opener([429, 429, 429, 429])
    with pytest.raises(urllib.error.HTTPError):
        sender.send("GLENMARK", "BUY", 5, opener=op, now=OPEN_AT)
    assert len(calls) == 4 and sender.last_retries == 3                                                     # 2, 4, 8 then gives up
    for code in (400, 401, 403, 500, 503):
        op, calls = _opener([code, "ok"])
        with pytest.raises(urllib.error.HTTPError) as e:
            sender.send("GLENMARK", "BUY", 5, opener=op, now=OPEN_AT)
        assert e.value.code == code and len(calls) == 1 and sender.last_retries == 0


def test_timeout_is_unknown_and_other_network_errors_raise(live):
    for exc in (TimeoutError(), urllib.error.URLError("timed out"), urllib.error.URLError(TimeoutError("x")),
                ConnectionResetError()):
        op, calls = _opener([exc, "ok"])
        with pytest.raises(sender.SendUnknown, match="check"):
            sender.send("GLENMARK", "BUY", 5, opener=op, now=OPEN_AT)
        assert len(calls) == 1                                                                               # never blindly retried
    op, calls = _opener([urllib.error.URLError("name resolution failed"), "ok"])
    with pytest.raises(urllib.error.URLError):                                                              # nothing went out: plain error
        sender.send("GLENMARK", "BUY", 5, opener=op, now=OPEN_AT)
    assert len(calls) == 1


def test_already_held_reply(live):
    op, calls = _opener(['{"status":"REJECTED_ALREADY_HELD"}'])
    with pytest.raises(sender.AlreadyHeld, match="REJECTED_ALREADY_HELD"):
        sender.send("TCS", "BUY", 1, opener=op, now=OPEN_AT)
    assert len(calls) == 1


# --- refusals -------------------------------------------------------------------------------------------------

def test_outside_hours_and_holiday_refused_before_anything_goes_out(live, monkeypatch):
    op, calls = _opener(["ok"] * 5)
    for t in ((9, 14), (15, 30), (16, 0), (3, 0)):
        with pytest.raises(sender.MarketClosed, match="outside market hours"):
            sender.send("TCS", "BUY", 1, opener=op, now=datetime(2026, 11, 12, *t, tzinfo=IST))
    sender.send("TCS", "BUY", 1, opener=op, now=datetime(2026, 11, 12, 9, 15, tzinfo=IST))
    sender.send("TCS", "EXIT", opener=op, now=datetime(2026, 11, 12, 15, 29, 59, tzinfo=IST))
    assert len(calls) == 2
    monkeypatch.setattr(hol, "is_trading_day", lambda d, holidays=None: d.weekday() < 5)
    with pytest.raises(sender.MarketClosed, match="not a trading day"):
        sender.send("TCS", "BUY", 1, opener=op, now=datetime(2026, 11, 15, 10, 0, tzinfo=IST))              # a Sunday
    assert len(calls) == 2
    assert sender.market_open(datetime(2026, 11, 15, 10, 0, tzinfo=IST)) == (False, "holiday")
    assert sender.market_open(datetime(2026, 11, 12, 10, 0, tzinfo=IST)) == (True, "open")
    assert sender.market_open(datetime(2026, 11, 12, 18, 0, tzinfo=IST)) == (False, "closed")


def test_no_token_refused(live, monkeypatch):
    monkeypatch.delenv(sender.ENV_TOKEN)
    op, calls = _opener(["ok"])
    with pytest.raises(RuntimeError, match="TRADETRON_FUTURES_TOKEN is not set"):
        sender.send("TCS", "BUY", 1, opener=op, now=OPEN_AT)
    assert calls == [] and sender.check()["token_set"] is False
    monkeypatch.setenv("TRADETRON_TOKEN", "the cash token is not the futures token")
    with pytest.raises(RuntimeError):
        sender.send("TCS", "BUY", 1, opener=op, now=OPEN_AT)
    assert calls == []


def test_token_never_logged_or_public(live):
    op, _ = _opener(["ok", "ok"])
    sender.send("M&M", "BUY", 1, opener=op, now=OPEN_AT)
    sender.kill_switch("k", opener=op)
    assert live["log"].lines and all(TOKEN not in line for line in live["log"].lines)
    assert any("sig_symbol" in line and "M&M" in line for line in live["log"].lines)
    b = sender.body("M&M", "BUY", 1, TOKEN)
    assert "auth-token" not in sender.public(b) and sender.public(b)["value2"] == "BUY"
    assert TOKEN not in json.dumps(sender.public(sender.kill_body(TOKEN)))
    c = sender.check(gap=12.5, now=OPEN_AT)
    assert c["token_set"] is True and TOKEN not in json.dumps(c)
    assert c == {"token_set": True, "url_host": "api.tradetron.tech", "gap_s": 12.5, "user_agent": "tt-webhook/1.0",
                 "window": {"open": "09:15", "close": "15:30", "state": "open", "ok": True}}
    assert sender.check(gap=2)["gap_s"] == 10.0


# --- the book on the real sender ------------------------------------------------------------------------------

def _real_book(monkeypatch):
    cfg, data, i = world(monkeypatch, long_real=True)
    cal = data["cal"]
    book.evening(cfg, cut(data, i), today=cal[i])
    monkeypatch.setattr(sender, "market_open", lambda now=None: (True, "open"))
    return cfg, cal[i + 1]


def _rows(day):
    with get_db() as conn:
        return conn.execute("SELECT symbol, action, status, reply FROM futures_orders WHERE day = ? AND account = 'real_long' "
                            "ORDER BY created_at, symbol", (day,)).fetchall()


def test_timeout_marks_unknown_check_and_never_resends(live, monkeypatch):
    cfg, day = _real_book(monkeypatch)
    op, calls = _opener([TimeoutError(), "ok", "ok", "ok"])                   # UP times out, AUX is accepted
    monkeypatch.setattr(urllib.request, "urlopen", op)
    out = book.send_orders(cfg, Kite({"UPFUT": 282.0, "AUXFUT": 283.0}), day)
    assert [x["symbol"] for x in out["sent"]] == ["AUX"] and [x["symbol"] for x in out["failed"]] == ["UP"]
    assert out["failed"][0]["unknown"] and out["failed"][0]["error"].startswith("unknown · check")
    rows = {r[0]: r for r in _rows(day)}
    assert rows["UP"][2] == "unknown · check" and "may have gone in" in rows["UP"][3] and rows["AUX"][2] == "sent"
    st = book._state()
    assert "UP" not in st["accounts"]["real_long"]["positions"] and "AUX" in st["accounts"]["real_long"]["positions"]
    assert [o["sent"] for o in st["plan"]["long"]] == ["unknown", "sent"]
    # the same day again, and a manual send: nothing is re-sent
    assert book.send_orders(cfg, Kite({"UPFUT": 282.0}), day)["sent"] == [] and len(calls) == 2
    with pytest.raises(ValueError, match="already unknown"):
        book.skip_order(cfg, "UP", False, now=datetime.fromisoformat(day + "T09:20:00").replace(tzinfo=IST))
    m = book.morning(cfg, datetime.fromisoformat(day + "T09:40:00").replace(tzinfo=IST))
    up = [o for o in m["orders"] if o["symbol"] == "UP"][0]
    assert up["status"] == "unknown · check" and not up["sent"] and not up["failed"]
    assert TOKEN not in json.dumps(book.summary(cfg)["log"])


def test_already_held_leaves_the_plan_unfilled(live, monkeypatch):
    cfg, day = _real_book(monkeypatch)
    op, calls = _opener(['{"status":"REJECTED_ALREADY_HELD"}', "ok"])
    monkeypatch.setattr(urllib.request, "urlopen", op)
    out = book.send_orders(cfg, Kite({"UPFUT": 282.0, "AUXFUT": 283.0}), day)
    assert [x["symbol"] for x in out["sent"]] == ["AUX"] and out["failed"][0]["symbol"] == "UP"
    assert out["failed"][0]["error"].startswith("already held at the deployment") and not out["failed"][0]["unknown"]
    rows = {r[0]: r for r in _rows(day)}
    assert rows["UP"][2] == "failed" and rows["UP"][3].startswith("already held at the deployment")
    st = book._state()
    assert "UP" not in st["accounts"]["real_long"]["positions"] and st["plan"]["long"][0]["sent"] == "failed"
    assert book.send_orders(cfg, Kite({"UPFUT": 282.0}), day)["sent"] == [] and len(calls) == 2            # no re-send


def test_stop_exit_timeout_keeps_the_lot_flagged(live, monkeypatch):
    cfg, day = _real_book(monkeypatch)
    op, calls = _opener(["ok", "ok", TimeoutError(), "ok"])
    monkeypatch.setattr(urllib.request, "urlopen", op)
    book.send_orders(cfg, Kite({"UPFUT": 282.0, "AUXFUT": 283.0}), day)
    st = book._state()
    stop = st["accounts"]["real_long"]["positions"]["UP"]["stop"]
    out = book.watch_stops(cfg, Kite({"UPFUT": stop - 1, "AUXFUT": 283.0}), day)
    hit = [h for h in out["hits"] if h["account"] == "real_long"][0]
    assert hit["unknown"] and hit["error"].startswith("unknown · check")
    pos = book._state()["accounts"]["real_long"]["positions"]["UP"]
    assert pos["check"] == f"{day}: EXIT unknown · check"                                                  # the lot stays, flagged
    assert book.watch_stops(cfg, Kite({"UPFUT": stop - 1, "AUXFUT": 283.0}), day)["hits"] == [] and len(calls) == 3   # never re-sent


def test_summary_shows_the_sender_check(live, monkeypatch):
    cfg, _, _ = world(monkeypatch)
    s = book.summary(cfg)["sender"]
    assert s["token_set"] is True and s["url_host"] == "api.tradetron.tech" and s["gap_s"] == 10.0
    assert s["window"]["open"] == "09:15" and s["window"]["close"] == "15:30" and s["window"]["state"] in ("open", "closed", "holiday")
    assert TOKEN not in json.dumps(book.summary(cfg))
