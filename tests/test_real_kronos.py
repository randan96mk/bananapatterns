"""📈 The candle model on the Real book's open positions (backend/market/real_reads.py) with a fake worker, hand-built
daily / 1-minute candles and a seeded Real book: reads per open name only, the daily and 9:20 AM rows (book "real",
the touch levels from the book), the scoring and the scorecard rows, the gate, the table payload in shadow vs cards
(the sort rule), the Home item only in cards, the two Telegram lines, the code verdicts, the speed guarantee (the
9:16 AM orders and the stop check never wait on a 5 s worker), Kite logged out, kronos off, the archive move."""

import json
import sqlite3
import threading
import time as _t
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.alerts import messages as msg
from backend.config import IST, AppConfig
from backend.database import archive
from backend.database.models import get_db
from backend.engine import attention
from backend.engine import real
from backend.market import candles as cd
from backend.market import chart
from backend.market import paths as pth
from backend.market import reads as rd
from backend.market import real_reads as rr
from tests.test_real import Kite, Sender

ROOT = Path(__file__).resolve().parent.parent
TODAY = "2026-11-19"                                         # a Thursday (trading day)
TOKENS = {"ABC": 9001, "DEF": 9002, "GHI": 9003}


def cfg(**market) -> AppConfig:
    c = AppConfig()
    c.ai.enabled = False
    for k, v in market.items():
        setattr(c.market, k, v)
    return c


def weekdays_until(end: str, n: int) -> list[str]:
    d, out = date.fromisoformat(end), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def seed_name(sym: str, base: float = 100.0, step: float = 0.5, n: int = 130, end: str = "2026-11-18") -> list[str]:
    tok = TOKENS[sym]
    days = weekdays_until(end, n)
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO instruments (instrument_token, exchange_token, tradingsymbol, name, exchange, segment, instrument_type, "
                     "tick_size, lot_size, fetched_on) VALUES (?,?,?,?,?,?,?,?,?,?)", (tok, tok, sym, sym, "NSE", "NSE", "EQ", 0.05, 1, "2026-11-01"))
        for i, d in enumerate(days):
            c = base + step * i
            conn.execute("INSERT OR REPLACE INTO candles_daily (instrument_token, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                         (tok, d, c - 0.2, c + 1, c - 1, c, 1000))
    return days


def add_daily(sym: str, rows: list[tuple]) -> None:
    """(date, open, high, low, close) after the seeded ones."""
    with get_db() as conn:
        for d, o, h, lo, c in rows:
            conn.execute("INSERT OR REPLACE INTO candles_daily (instrument_token, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                         (TOKENS[sym], d, o, h, lo, c, 1000))


def book(positions: dict) -> None:
    st = {"started": "2026-11-01", "capital": 300000.0, "cash": 100000.0, "charges": 0.0, "as_of": "2026-11-18", "positions": {}, "orders": []}
    for sym, (px, stop, extra) in positions.items():
        st["positions"][sym] = {"qty": 10, "px": px, "d": "2026-11-10", "stop": stop, "last": px, "charges": 10.0, "days": 5, "ts": sym,
                                "variant": "E", "est": False, "confirmed": True, **extra}
    real._save(st)


def fake(p_up: dict, stop_first: dict, target_first: dict = None):
    calls = []

    def w(payload):
        calls.append(payload)
        hs = payload.get("horizons") or [payload["horizon"]]
        last = payload["candles"][-1]["c"]
        lv = payload.get("levels") or []
        sym = payload["symbol"]
        touch = {}
        if lv:
            touch[pth.level_key(lv[0])] = stop_first.get(sym, 0.2)
        if len(lv) > 1:
            touch[pth.level_key(lv[1])] = (target_first or {}).get(sym, 0.3)
        reads = [{"horizon": h, "p_up": p_up.get(sym, 0.6), "exp_range": 3.5, "paths_summary": {"q10": [last - 2] * h, "q50": [last] * h, "q90": [last + 2] * h},
                  "touch": touch, "touch_any": touch, "samples": payload["samples"]} for h in hs]
        return {**reads[-1], "reads": reads, "ms": 120, "version": rd.VERSION, "last": last, "interval": payload.get("interval"), "context": len(payload["candles"])}
    return w, calls


@pytest.fixture
def world(monkeypatch):
    for s, b in (("ABC", 100.0), ("DEF", 200.0), ("GHI", 300.0)):
        seed_name(s, b)
    book({"ABC": (150.0, 140.0, {}), "DEF": (260.0, 262.0, {"target": 300.0})})       # GHI: closed (not in the book)
    with get_db() as conn:
        conn.execute("INSERT INTO real_trades (payload) VALUES (?)", (json.dumps({"symbol": "GHI", "qty": 5, "net": 10.0}),))
    w, calls = fake({"ABC": 0.62, "DEF": 0.44}, {"ABC": 0.31, "DEF": 0.52}, {"DEF": 0.12})
    monkeypatch.setattr(rd, "WORKER", w)
    return calls


def now_at(hhmm: str, d: str = TODAY) -> datetime:
    h, m = hhmm.split(":")
    return datetime.combine(date.fromisoformat(d), datetime.min.time().replace(hour=int(h), minute=int(m)), IST)


# --- the nightly read ------------------------------------------------------------------------------------------------------

def test_nightly_reads_each_open_name_once_with_the_books_levels(world):
    c = cfg()
    out = rr.nightly(c, now_at("19:05", "2026-11-18"))
    assert out["read"] == ["ABC", "DEF"] and "GHI" not in json.dumps(out)                  # closed positions skipped
    assert [p["symbol"] for p in world] == ["ABC", "DEF"]
    a, d = world
    assert a["interval"] == "1d" and a["horizons"] == [1, 3, 5] and a["samples"] == c.market.samples and len(a["candles"]) == rr.DAILY_BARS
    assert a["levels"] == [140.0] and d["levels"] == [262.0, 300.0]                         # stop (and target) from the book
    rows = rd.rows("book = 'real'")
    assert len(rows) == 6 and {r["interval"] for r in rows} == {"1d"} and all(r["hint_id"] is None for r in rows)
    assert sorted({r["horizon"] for r in rows}) == [1, 3, 5] and rows[0]["version"] == "kronos-mini-v1"
    assert rows[0]["paths_json"]["asof"] == "2026-11-18" and rows[0]["paths_json"]["levels"] == {"stop": 140.0, "target": None}
    assert rr.nightly(c, now_at("19:30", "2026-11-18"))["read"] == [] and len(world) == 2   # once per candle date
    assert rd.latest("ABC", 3) is None                                                    # the Tips / index lookups never see them


def test_tips_index_scoring_leaves_the_real_rows_alone(world):
    rr.nightly(cfg(), now_at("19:05", "2026-11-18"))
    assert rd.score(now_at("18:40", "2026-11-30")) == {"scored": 0, "missing": 0, "waiting": 0}


# --- the 9:20 AM read ------------------------------------------------------------------------------------------------------

class Hist:
    def __init__(self, has_token=True):
        self.has_token, self.calls = has_token, []

    def historical_minute(self, token, start, end, label=""):
        self.calls.append((token, start.isoformat(), end.isoformat()))
        out = []
        for d in weekdays_until(end.isoformat(), 3):
            if d < start.isoformat():
                continue
            base = 250.0 if d < TODAY else 255.0                                          # today: a gap up from ~250
            n = 375 if d < TODAY else 6
            for i in range(n):
                m = 9 * 60 + 15 + i
                c = base + 0.01 * i
                out.append({"date": datetime(*map(int, d.split("-")), m // 60, m % 60, tzinfo=IST), "open": c, "high": c + 0.1, "low": c - 0.1,
                            "close": c, "volume": 100})
        return out


def test_920_read_on_minute_candles_with_the_gap_fact(world):
    c = cfg()
    h = Hist()
    assert rr.due_920(c, h, now_at("09:19")) is False and rr.due_920(c, h, now_at("09:21")) is True
    out = rr.run_920(c, h, now_at("09:21"))
    assert out["read"] == ["ABC", "DEF"] and rr.due_920(c, h, now_at("09:22")) is False   # once a day
    p = world[-1]
    assert p["interval"] == "5m" and p["horizon"] == 74 and p["levels"] == [262.0, 300.0]
    assert p["candles"][-1]["at"] == f"{TODAY} 09:15"                                       # nothing after 9:20 AM goes in
    r = rd.rows("book = 'real' AND interval = '1m' AND symbol = 'DEF'")[0]
    assert r["at"] == f"{TODAY}T09:20:00+05:30" and r["horizon"] == rr.H_920 and r["hint_id"] is None
    g = r["paths_json"]["gap"]
    assert g["dir"] == "up" and g["state"] == "holding" and g["text"] == "gap up 0.5 % held at 9:20 AM" and g["text"].endswith("held at 9:20 AM")


def test_gap_fact_filled_holding_and_none():
    t = lambda o, h, lo: [{"t": "09:15", "o": o, "h": h, "l": lo, "c": o}, {"t": "09:19", "o": o, "h": h, "l": lo, "c": o}]
    assert rr.gap_fact(100.0, t(98.0, 100.5, 97.5))["text"] == "gap down 2.0 % filled by 9:20 AM"
    assert rr.gap_fact(100.0, t(98.0, 99.0, 97.5))["state"] == "holding"
    assert rr.gap_fact(100.0, t(100.1, 100.2, 99.9))["text"] == "no gap at the open"
    assert rr.gap_fact(None, t(1, 1, 1)) is None


def test_logged_out_no_920_read_and_no_error(world):
    c = cfg()
    h = Hist(has_token=False)
    assert rr.due_920(c, h, now_at("09:21")) is False
    assert rr.run_920(c, h, now_at("09:21")) == {"fetched": {}, "read": []} and h.calls == [] and world == []
    svc = rd.MarketService(SimpleNamespace(config=c, client=h))
    assert svc.tick(now_at("09:21"), wait=True)["why"] is None and rd.rows("book = 'real'") == []


def test_the_market_loop_runs_the_920_read_once(world):
    c = cfg()
    h = Hist()
    h.quote = lambda keys: {}
    svc = rd.MarketService(SimpleNamespace(config=c, client=h))
    svc.tick(now_at("09:21"), wait=True)
    assert len(rd.rows("book = 'real' AND interval = '1m'")) == 2
    svc.tick(now_at("09:22"), wait=True)
    assert len(rd.rows("book = 'real' AND interval = '1m'")) == 2


# --- scoring, the scorecard, the gate --------------------------------------------------------------------------------------

def test_daily_scoring_from_the_stored_candles(world):
    c = cfg()
    rr.nightly(c, now_at("19:05", "2026-11-18"))
    last = rd.rows("book = 'real' AND symbol = 'DEF'")[0]["paths_json"]["last"]
    assert rr.score(now_at("19:05", "2026-11-19"))["waiting"] == 6
    add_daily("DEF", [("2026-11-19", last, last + 1, 261.0, last - 3)])                     # day 1: down, the stop (262) touched
    add_daily("ABC", [("2026-11-19", 164, 166, 163, 165.5)])
    out = rr.score(now_at("19:05", "2026-11-19"))
    assert out == {"scored": 2, "missing": 0, "waiting": 4}
    r1 = {r["symbol"]: r for r in rd.rows("book = 'real' AND horizon = 1")}
    o = r1["DEF"]["outcome_json"]
    assert o["up"] is False and o["direction_right"] is True and o["first"] == pth.level_key(262.0) and o["touch_right"] is True and o["stop_touched"]
    assert r1["ABC"]["outcome_json"]["direction_right"] is True
    add_daily("DEF", [("2026-11-20", 259, 260, 255, 256), ("2026-11-23", 256, 257, 250, 251)])
    add_daily("ABC", [("2026-11-20", 166, 167, 165, 166), ("2026-11-23", 166, 168, 165, 167)])
    assert rr.score(now_at("19:05", "2026-11-23"))["scored"] == 2                           # 3 d scored, 5 d still waiting
    sc = rr.scorecard()
    assert [(x["name"], x["horizon"]) for x in sc] == [("Real · daily", 1), ("Real · daily", 3), ("Real · daily", 5)]
    assert rd.card_row(sc, "Real · daily", 3)["direction_pct"] == 100.0 and rd.group_label("Real · daily", "1d", 3) == "Real · daily · 3 d"
    assert rr.score(now_at("19:05", "2026-12-20"))["missing"] == 2                          # never reached 5 sessions: missing


def test_920_scoring_from_the_days_minute_candles_and_the_scorecard_row(world):
    c = cfg()
    rr.run_920(c, Hist(), now_at("09:21"))
    tok = TOKENS["DEF"]
    bars = [{"t": f"{(9 * 60 + 20 + i) // 60:02d}:{(9 * 60 + 20 + i) % 60:02d}", "o": 255.0, "h": 255.5, "l": 254.5 - 0.01 * i, "c": 255.0 - 0.02 * i, "v": 1}
            for i in range(370)]
    cd.store(tok, TODAY, bars)
    out = rr.score(now_at("18:40"))
    assert out["scored"] == 1 and out["waiting"] == 1                                       # ABC has no full day yet: waits (no client)
    r = rd.rows("book = 'real' AND interval = '1m' AND symbol = 'DEF'")[0]
    assert r["outcome_json"]["up"] is False and r["outcome_json"]["direction_right"] is True and r["outcome_json"]["bars"] == 370
    assert [x["name"] for x in rd.scorecard()] == ["Real · 9:20 AM"]
    assert rr.score(now_at("18:40") + timedelta(days=1))["missing"] == 1


def put_real(sym="ABC", h=3, p_up=0.6, right=None, at_="2026-11-10T19:05:00+05:30"):
    with get_db() as conn:
        rid = conn.execute("INSERT INTO market_reads (at, symbol, interval, horizon, p_up, exp_range, touch_json, paths_json, version, ms, hint_id, book) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (at_, sym, "1d", h, p_up, 3.0, "{}", json.dumps({"last": 1.0, "asof": at_[:10]}), rd.VERSION, 100, None, "real")).lastrowid
        if right is not None:
            conn.execute("UPDATE market_reads SET scored_at = 'x', outcome_json = ? WHERE id = ?", (json.dumps({"direction_right": right, "touch_right": None}), rid))
    return rid


def test_the_gate_scorecard_or_the_switch():
    assert rr.gate(cfg())["show"] is False and rr.gate(cfg())["mode"] == "shadow"
    assert rr.gate(cfg(kronos_show="cards"))["show"] is True and "kronos_show = cards" in rr.gate(cfg(kronos_show="cards"))["why"]
    for i in range(100):
        put_real(right=i < 57)
    g = rr.gate(cfg())
    assert g["show"] is True and g["earned"] is True and g["mode"] == "cards"
    assert rr.chip(cfg()) == "candle model · cards · daily hit rate 57 % over 100 reads"
    for _ in range(10):
        put_real(right=False)
    assert rr.gate(cfg())["show"] is False and rr.chip(cfg()).startswith("candle model · shadow · daily hit rate 52 %")
    assert rr.gate(cfg(kronos=False))["mode"] == "off" and rr.chip(cfg(kronos=False)) == "candle model · off"
    assert rd.card_row(rd.scorecard(), "NIFTY", 30) is None and rd.gate(cfg(kronos_show="cards"))["cards"] is False   # the Tips gate: unchanged


# --- what shows: the table, Home, Telegram ---------------------------------------------------------------------------------

def test_table_payload_shadow_hides_the_numbers_cards_sorts_by_stop_first(world):
    rr.nightly(cfg(), now_at("19:05", "2026-11-18"))
    t = rr.table(cfg(), now_at("10:00"))
    assert t["mode"] == "shadow" and t["show"] is False and t["order"] is None
    assert all(r["shadow"] and "stop_first" not in r and "up" not in r for r in t["rows"].values())
    assert "52" not in json.dumps(t["rows"]) and t["rows"]["ABC"]["code"]["head"].startswith("up")      # the code read still shows
    assert "hidden until the Real · daily scorecard passes 55 % over 100 reads" in t["footer"]
    c = rr.table(cfg(kronos_show="cards"), now_at("10:00"))
    assert c["show"] is True and c["order"] == ["DEF", "ABC"]                               # stop-first desc: 52 % then 31 %
    assert (c["rows"]["DEF"]["stop_first"], c["rows"]["DEF"]["target_first"], c["rows"]["DEF"]["up"]) == (52, 12, 44)
    assert c["rows"]["ABC"]["stop_first"] == 31 and c["rows"]["ABC"]["up"] == 62 and c["rows"]["ABC"]["version"] == "kronos-mini-v1"
    assert c["chip"] == "candle model · cards · daily not scored yet" and "Kronos-mini" in c["footer"]
    assert set(c["rows"]) == {"ABC", "DEF"}


def test_home_item_only_in_cards(world):
    rr.nightly(cfg(), now_at("19:05", "2026-11-18"))
    rr.run_920(cfg(), Hist(), now_at("09:21"))
    assert rr.attention_items(cfg(), now_at("10:00")) == []                                 # shadow: stored only
    items = rr.attention_items(cfg(kronos_show="cards"), now_at("10:00"))
    assert items == [("info", "DEF · candle model: stop 262 touched first in 52 % of 3-day paths · gap up 0.5 % held at 9:20 AM",
                      "informational · the stop check decides as always · Portfolio › Real")]
    p = attention._pack(items)
    assert p["level"] == "info" and p["items"][0]["meta"].startswith("informational")


def test_the_two_telegram_lines(world):
    assert rr.brief_line(cfg(), now_at("09:00")) is None                                    # no read yet
    rr.nightly(cfg(), now_at("19:05", "2026-11-18"))
    line = rr.brief_line(cfg(), now_at("09:00"))
    assert line == "candle model (shadow): 1 of 2 held names lean down · nearest to its stop: DEF (52 % odds of the stop first)"
    assert rr.brief_line(cfg(kronos_show="cards"), now_at("09:00")).startswith("candle model (cards):")
    text = msg.ai_brief(TODAY, "09:00", ["one line"], [line])
    assert "📈 candle model (shadow): 1 of 2" in text and text.count("AI ·") == 1               # the code line carries no AI mark
    assert rr.day_line(cfg(), now_at("15:40")) is None                                     # nothing read today, nothing scored
    rr.run_920(cfg(), Hist(), now_at("09:21"))
    for r in rd.rows("book = 'real' AND interval = '1m'"):
        with get_db() as conn:
            conn.execute("UPDATE market_reads SET scored_at = 'x', outcome_json = ? WHERE id = ?",
                         (json.dumps({"direction_right": r["symbol"] == "DEF"}), r["id"]))
    for i in range(140):
        put_real(right=i < 80)
    day = rr.day_line(cfg(), now_at("15:40"))
    assert day == "candle model: 9:20 AM read was right on 1 of 2 · scorecard 57 % (140)"
    from backend.alerts import daysum
    parts = daysum.parts(cfg(), TODAY)
    assert parts["kronos"] == {"line": day}
    s = msg.day_summary(parts, TODAY)
    assert "📈 candle model: 9:20 AM read was right on 1 of 2" in s and "[AI]" not in s
    assert rr.brief_line(cfg(kronos=False)) is None and rr.day_line(cfg(kronos=False), now_at("15:40")) is None


# --- the code read --------------------------------------------------------------------------------------------------------

def _bars(closes, opens=None, vol=None):
    out = []
    for i, c in enumerate(closes):
        o = (opens or {}).get(i, closes[i - 1] if i else c)
        out.append({"date": f"2026-10-{i + 1:02d}" if i < 31 else f"2026-11-{i - 30:02d}", "open": o, "high": max(o, c) + 1, "low": min(o, c) - 1,
                    "close": c, "volume": (vol or {}).get(i, 1000)})
    return out


def test_code_verdicts_table():
    up = _bars([100 + 2 * i for i in range(40)])
    r = chart.daily_read(up, stop=100.0)
    assert r["side"] == "up" and r["higher_lows"] and r["head"] == "up · higher lows" and r["verdict"] == "above the 10-day range"
    assert chart.daily_read(up, stop=175.0)["verdict"] == "stop within 1-day range"                     # 178 − 175 ≤ the 1-day range (4)
    flat = _bars([100 + (1 if i % 2 else -1) for i in range(40)])
    f = chart.daily_read(flat, stop=80.0)
    assert f["side"] == "sideways" and f["verdict"] == "inside the range" and f["head"].startswith("sideways · 98–102")
    assert chart.daily_read(flat, 80.0, 120.0, {"stop_first": 10, "target_first": 31, "up": 60})["verdict"] == "target before stop 3:1"
    assert chart.daily_read(flat, 80.0, None, {"stop_first": 10, "target_first": None, "up": 51})["verdict"] == "coin flip"
    assert chart.daily_read(flat, 80.0, None, None)["verdict"] == "inside the range"                   # no model: code only
    down = _bars([200 - 2 * i for i in range(40)], opens={39: 121.0}, vol={39: 300})
    d = chart.daily_read(down, stop=100.0)
    assert d["side"] == "down" and d["head"] == "down · lower highs · gap down 2.4 %" and d["verdict"] == "below the 10-day range"
    low = _bars([200 - 2 * i for i in range(40)], vol={39: 300})
    assert chart.daily_read(low)["head"] == "down · lower highs · low volume"
    assert set(chart.DAILY) >= {"stop_days", "ratio", "coin", "gap_pct", "vol_low"}


# --- the speed guarantee, off, the archive, the README -------------------------------------------------------------------

def _orders_state():
    st = {"started": "2026-11-01", "capital": 300000.0, "cash": 200000.0, "charges": 0.0, "as_of": "2026-11-18", "orders": [
        {"action": "EXIT", "symbol": "ABC", "ts": "ABC", "qty": 10, "why": "trend exit"},
        {"action": "BUY", "symbol": "GHI", "ts": "GHI", "qty": 5, "stop": 280.0, "close": 300.0, "why": "breakout"}],
        "positions": {s: {"qty": 10, "px": px, "d": "2026-11-10", "stop": stop, "last": px, "charges": 10.0, "days": 5, "ts": s, "variant": "E",
                          "est": False, "confirmed": True} for s, px, stop in (("ABC", 150.0, 140.0), ("DEF", 260.0, 262.0))}}
    real._save(st)


def test_orders_and_the_stop_check_never_wait_on_a_5_s_worker(world, monkeypatch):
    monkeypatch.setenv("TRADETRON_TOKEN", "t")
    c = cfg()
    _orders_state()
    s0 = Sender()
    base = real.send_orders(c, None, TODAY, s0)
    base_stop = real.watch_stops(c, Kite({"DEF": 261.0}), TODAY, Sender())
    _orders_state()
    with get_db() as conn:
        conn.execute("DELETE FROM real_orders")
        conn.execute("DELETE FROM real_trades WHERE json_extract(payload, '$.symbol') != 'GHI'")
    started = threading.Event()

    def slow(payload):
        started.set()
        _t.sleep(5)
        return None
    monkeypatch.setattr(rd, "WORKER", slow)
    t = rr.start_nightly(c)                                                                  # a read in flight, holding the worker
    assert t is not None and started.wait(2)
    t0 = _t.monotonic()
    s1 = Sender()
    out = real.send_orders(c, None, TODAY, s1)                                               # the 9:16 AM orders
    took = _t.monotonic() - t0
    t1 = _t.monotonic()
    stop = real.watch_stops(c, Kite({"DEF": 261.0}), TODAY, Sender())                        # the stop check
    took2 = _t.monotonic() - t1
    assert took < 1.0 and took2 < 1.0 and t.is_alive()
    assert s1.calls == s0.calls and [x["symbol"] for x in out["sent"]] == [x["symbol"] for x in base["sent"]]
    assert [h["symbol"] for h in stop["hits"]] == [h["symbol"] for h in base_stop["hits"]] == ["DEF"]
    src = (ROOT / "backend/engine/real.py").read_text()
    assert "backend.market" not in src and "real_reads" not in src and "call_worker" not in src
    t.join(10)


def test_kronos_off_nothing(world):
    c = cfg(kronos=False)
    assert rr.nightly(c)["why"] == "off" and rr.start_nightly(c) is None and world == []
    assert rr.due_920(c, Hist(), now_at("09:21")) is False and rr.run_920(c, Hist(), now_at("09:21"))["read"] == []
    t = rr.table(c, now_at("10:00"))
    assert t["on"] is False and t["rows"] == {} and rr.attention_items(c) == []
    assert rd.rows("book = 'real'") == []


def test_archive_moves_real_rows_with_their_book_never_deletes():
    old = (datetime.now(IST) - timedelta(days=200)).isoformat(timespec="seconds")
    r_old, r_new = put_real(at_=old), put_real(at_=datetime.now(IST).isoformat(timespec="seconds"))
    out = archive.run(AppConfig())
    assert out["moved_by_table"]["market_reads"] == 1 and [r["id"] for r in rd.rows()] == [r_new]
    with sqlite3.connect(str(archive.archive_path())) as a:
        assert a.execute("SELECT id, book FROM market_reads").fetchall() == [(r_old, "real")]


def test_route_and_readme(world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api import routes
    from backend.api.routes import router
    rr.nightly(cfg(), now_at("19:05", "2026-11-18"))
    app = FastAPI()
    app.include_router(router)
    routes.set_state(routes.AppState(cfg(), Hist(has_token=False)))
    try:
        j = TestClient(app).get("/api/real/kronos").json()
        assert j["mode"] == "shadow" and set(j["rows"]) == {"ABC", "DEF"} and j["order"] is None
    finally:
        routes.set_state(None)
    t = (ROOT / "README.md").read_text()
    s = t[t.index("## 📈 Market read"):t.index("## REST API")]
    for w in ("Real positions", "9:20 AM", "Real · daily", "book", "kronos_show = cards", "stop check"):
        assert w in s, w
