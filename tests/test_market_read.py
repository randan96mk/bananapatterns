"""📈 The market read (backend/market) with a fake worker and hand-built candles: the chart facts by code, the worker
client (timeout / bad JSON → None), the reads store, hint reads (premium vs index), touch probabilities from paths, the
6:40 PM scoring and the scorecard, the cards gate, the [AI] words (4 lines, a bad figure dropped, the verdict by code),
the entry-timing block, the grade step, the speed guarantee (a 5 s worker never delays an arm or an order), the off
switch, the Kite login, the archive move. One real-engine test runs only where torch and the Kronos-mini weights are."""

import importlib.util
import json
import socket
import sqlite3
import threading
import time as _t
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config import IST, AppConfig
from backend.database import archive
from backend.database.models import get_db, init_db
from backend.market import candles as cd
from backend.market import chart
from backend.market import paths as pth
from backend.market import reads as rd
from backend.market import view as mv
from backend.market import words as mw
from backend.tips import ai4
from backend.tips import book as bk
from backend.tips import kite_orders as ko
from tests.test_tips_kite_auto import CALL2, D19, S82300, auto_setup, post, pushed  # noqa: F401
from tests.tips_world import at

ROOT = Path(__file__).resolve().parent.parent
REAL_QUEUE = rd.queue_hint
DAYS = ("2026-11-17", "2026-11-18", "2026-11-19")        # Tue, Wed, Thu (trading days)


def minute(i: int) -> str:
    m = 9 * 60 + 15 + i
    return f"{m // 60:02d}:{m % 60:02d}"


def day_bars(start: float, step: float, n: int = 375, wig: float = 0.0, d: str = DAYS[-1]) -> list[dict]:
    out = []
    for i in range(n):
        c = start + step * i + (wig if i % 2 else -wig)
        o = c - step / 2
        out.append({"d": d, "t": minute(i), "o": o, "h": max(o, c) + 1, "l": min(o, c) - 1, "c": c, "v": 0})
    return out


def seed_index(sym: str = "NIFTY", base: float = 25700.0, step: float = 0.2, today_n: int = 375) -> list[dict]:
    """Three sessions of a steady rise (the last one `today_n` minutes long), stored in candles_intraday."""
    bars = []
    px = base
    for i, d in enumerate(DAYS):
        n = today_n if i == len(DAYS) - 1 else 375
        b = day_bars(px, step, n, 0.3, d)
        cd.store(cd.TOKENS[sym], d, [{k: x[k] for k in ("t", "o", "h", "l", "c", "v")} for x in b], complete=i < 2)
        bars += b
        px = b[-1]["c"]
    return bars


def fake_result(payload: dict, p_up: float = 0.64, touch=None) -> dict:
    hs = payload.get("horizons") or [payload["horizon"]]
    last = payload["candles"][-1]["c"]
    reads = [{"horizon": h, "p_up": p_up, "exp_range": 12.5, "paths_summary": {"q10": [last - 5] * h, "q50": [last] * h, "q90": [last + 5] * h},
              "touch": touch if touch is not None else {pth.level_key(x): 0.2 for x in payload.get("levels") or []},
              "touch_any": {pth.level_key(x): 0.57 for x in payload.get("levels") or []}, "samples": payload["samples"]} for h in hs]
    top = reads[-1]
    return {**top, "reads": reads, "ms": 412, "version": rd.VERSION, "last": last, "interval": "1m", "context": min(120, len(payload["candles"]))}


@pytest.fixture
def worker(monkeypatch):
    calls = []

    def w(payload):
        calls.append(payload)
        return fake_result(payload)
    monkeypatch.setattr(rd, "WORKER", w)
    return calls


def cfg(**market) -> AppConfig:
    c = AppConfig()
    c.ai.enabled = False
    for k, v in market.items():
        setattr(c.market, k, v)
    return c


def now_at(hhmm: str, d: str = DAYS[-1]) -> datetime:
    return at(hhmm, date.fromisoformat(d))


# --- chart.py ---------------------------------------------------------------------------------------------------------------

def test_vwap_volume_weighted_and_equal_weights_without_volume():
    b = [{"d": "x", "t": "09:15", "o": 10, "h": 12, "l": 8, "c": 10, "v": 100}, {"d": "x", "t": "09:16", "o": 20, "h": 22, "l": 18, "c": 20, "v": 300}]
    assert chart.vwap(b) == 17.5                                          # (10×100 + 20×300) / 400
    assert chart.vwap([{**x, "v": 0} for x in b]) == 15.0                  # an index: no volume, equal weights


def test_averages_trend_and_opening_range_break_time():
    bars = seed_index()
    tr = chart.trend(bars)
    assert tr["side"] == "up" and tr["above_vwap"] and tr["avg_cross"] == "20 > 50" and tr["hh_ll"]["kind"] == "higher highs"
    closes5 = [x["c"] for x in chart.five(bars)]
    assert tr["avg20"] == round(sum(closes5[-20:]) / 20, 2) and tr["avg50"] == round(sum(closes5[-50:]) / 50, 2)
    o = tr["or"]
    assert o["high"] == round(max(x["h"] for x in bars if x["d"] == DAYS[-1] and x["t"] < "09:30"), 2)
    first = next(x for x in bars if x["d"] == DAYS[-1] and x["t"] >= "09:30" and x["c"] > o["high"])
    assert o["break_dir"] == "up" and o["break_at"] == first["t"]


def b5(t, o, h, lo, c, d="2026-11-19"):
    return {"d": d, "t": t, "o": o, "h": h, "l": lo, "c": c, "v": 0}


def test_swing_support_and_resistance_from_five_minute_pivots():
    # a 5-minute zig-zag: a pivot high at 100, a pivot low at 90, the last close 95
    seq = [(92, 93, 91, 92), (93, 96, 92, 95), (95, 100, 94, 98), (97, 98, 93, 94), (94, 95, 92, 93), (93, 94, 90, 91), (91, 94, 91, 93), (93, 96, 92, 95), (95, 96, 94, 95)]
    one = []
    for i, (o, h, lo, c) in enumerate(seq):                         # each 5-minute bar as 5 one-minute bars
        for j in range(5):
            one.append({"d": "2026-11-19", "t": minute(i * 5 + j), "o": o if j == 0 else c, "h": h, "l": lo, "c": c, "v": 0})
    lv = chart.levels(one, "NIFTY")
    assert lv["resistance"] == 100 and lv["support"] == 90 and lv["day"] == {"h": 100, "l": 90}
    assert lv["round"]["step"] == 100


def test_each_pattern_on_hand_built_candles():
    names = lambda bs, lv=None: [p["name"] for p in chart.patterns(bs, lv)]   # noqa: E731
    assert "bullish engulfing" in names([b5("09:15", 10, 10.5, 8.8, 9), b5("09:20", 8.9, 11, 8.8, 10.8)])
    assert "bearish engulfing" in names([b5("09:15", 9, 10.2, 8.9, 10), b5("09:20", 10.1, 10.2, 8.5, 8.7)])
    assert "pin bar (bullish)" in names([b5("09:15", 10, 10.2, 7, 10.1)])
    assert "pin bar (bearish)" in names([b5("09:15", 10, 13, 9.9, 9.95)])
    assert "inside bar" in names([b5("09:15", 10, 12, 8, 11), b5("09:20", 10.5, 11.5, 9, 10)])
    assert "three soldiers" in names([b5("09:15", 10, 11.1, 9.9, 11), b5("09:20", 11, 12.1, 10.9, 12), b5("09:25", 12, 13.1, 11.9, 13)])
    assert "three crows" in names([b5("09:15", 13, 13.1, 11.9, 12), b5("09:20", 12, 12.1, 10.9, 11), b5("09:25", 11, 11.1, 9.9, 10)])
    assert "doji at a level" in names([b5("09:15", 100, 101, 99, 100.05)], [100.0])
    assert "doji at a level" not in names([b5("09:15", 100, 101, 99, 100.05)], [120.0])
    base = [b5(minute(i * 5), 100, 100.5, 99.5, 100.1) for i in range(10)]
    pole = b5(minute(50), 100, 104, 99.8, 103.8)
    flag = [b5(minute(55 + 5 * k), 103.5, 103.8, 102.8, 103.2) for k in range(3)]
    brk = b5(minute(70), 103.3, 105, 103.2, 104.8)
    f = [p for p in chart.patterns(base + [pole] + flag + [brk]) if p["name"] == "bull flag"]
    assert f and f[0]["bars"] == 3 and f[0]["start"] == minute(55) and f[0]["at"] == minute(65) and f[0]["broke"] == minute(70)
    assert f[0]["text"] == "3 small inside candles after the breakout at 10:05 AM · broke up at 10:25 AM"          # 12-hour IST


def test_atr_and_expected_range():
    bs = [b5(minute(i * 5), 100, 102, 98, 100) for i in range(20)]
    assert chart.atr(bs) == 4.0
    assert chart.expected_range(4.0, 5) == 4.0 and chart.expected_range(4.0, 30) == round(4.0 * 6 ** 0.5, 1)


# --- the worker client --------------------------------------------------------------------------------------------------------

def _serve(body: bytes, delay: float = 0.0):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            _t.sleep(delay)
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_worker_client_timeout_bad_json_and_down_give_none():
    c = cfg()
    slow = _serve(b'{"p_up": 0.6}', delay=1.0)
    c.market.port = slow.server_address[1]
    t0 = _t.monotonic()
    assert rd.call_worker(c, {"x": 1}, timeout=0.3) is None and _t.monotonic() - t0 < 0.9      # timed out → None
    slow.shutdown()
    bad = _serve(b"not json{")
    c.market.port = bad.server_address[1]
    assert rd.call_worker(c, {"x": 1}, timeout=2) is None                                         # bad JSON → None
    bad.shutdown()
    empty = _serve(b'{"error": "model loading"}')
    c.market.port = empty.server_address[1]
    assert rd.call_worker(c, {"x": 1}, timeout=2) is None                                         # no numbers → None
    empty.shutdown()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    c.market.port = s.getsockname()[1]
    s.close()
    assert rd.call_worker(c, {"x": 1}, timeout=1) is None                                         # nobody listening → None


# --- reads --------------------------------------------------------------------------------------------------------------------

def test_index_read_stores_both_horizons_with_the_version(worker):
    seed_index()
    c = cfg()
    ids = rd.read_index(c, "NIFTY", now_at("15:29"))
    assert len(ids) == 2 and len(worker) == 1                                                     # one call, the 15 cut from the 30
    p = worker[0]
    assert p["horizon"] == 30 and p["horizons"] == [15, 30] and p["samples"] == 10 and len(p["candles"]) == 375 * 3
    rs = rd.rows()
    assert [r["horizon"] for r in rs] == [15, 30] and all(r["version"] == "kronos-mini-v1" and r["ms"] == 412 for r in rs)
    assert rs[0]["paths_json"]["basis"] == "index" and rs[0]["paths_json"]["last"] == p["candles"][-1]["c"] and rs[1]["p_up"] == 0.64


def test_worker_down_stores_no_row_and_the_panel_says_no_model_read(monkeypatch):
    seed_index()
    monkeypatch.setattr(rd, "WORKER", lambda p: None)
    c = cfg(kronos_show="cards")
    assert rd.read_index(c, "NIFTY", now_at("15:29")) == [] and rd.rows() == []
    v = mv.panel(c, "NIFTY", now_at("15:29"))
    assert v["tiles"]["model"]["head"] == "no model read" and v["tiles"]["trend"]["head"] == "up · above VWAP"


def hint(raw="BUY NIFTY 25800 CE 180-185 SL 150 TGT 230/260", entry=None, kite=None, status="active", side="BUY", typ="CE") -> dict:
    with get_db() as conn:
        hid = conn.execute("INSERT INTO tips_hints (channel, post_id, created, posted_at, raw, source, status, events) VALUES (?,?,?,?,?,?,?,?)",
                           ("aakankshatrading", int(_t.time() * 1000) % 10 ** 9, "2026-11-19T10:03:00+05:30", "2026-11-19T10:03:00+05:30", raw, "live", status, "[]")).lastrowid
    h = bk.load(hid)
    e = entry or {"mode": "zone", "lo": 180.0, "hi": 185.0}
    plan = {"ok": True, "entry": e, "stop": {"kind": "premium", "value": 150.0}, "targets": [230.0, 260.0], "entry_text": "zone 180–185",
            "stop_text": "stop 150", "targets_text": "230 / 260", "time_stop": "15:14"}
    h.update(symbol="NIFTY", side=side, instrument="option", strike=25800.0, opt_type=typ, lot_size=65, exchange="NFO", tradingsymbol="NIFTY26NOV25800CE",
             plan=plan, entry=e, stop=plan["stop"], targets=plan["targets"], status=status)
    bk.save(h)
    if kite:
        h["kite"] = {**ko.new_kite(), **kite}
        ko.save_kite(h)
    return bk.load(hid)


def ticks(hid: int, n: int, start: float = 180.0, step: float = 0.1, d: str = DAYS[-1]):
    with get_db() as conn:
        for i in range(n):
            for s in (0, 30):
                conn.execute("INSERT OR IGNORE INTO tips_ticks (hint_id, at, ltp) VALUES (?,?,?)",
                             (hid, f"{d}T{minute(i)}:{s:02d}+05:30", start + step * i + (0.05 if s else 0)))


def test_hint_read_uses_premium_candles_from_60_bars_else_the_index(worker):
    seed_index()
    c = cfg()
    h = hint()
    ticks(h["id"], 59)
    rd.read_hint(c, h["id"], "ingest", now_at("15:29"))
    assert worker[-1]["symbol"] == "NIFTY" and worker[-1]["levels"] == [] and worker[-1]["horizons"] == [10, 15]
    assert {r["paths_json"]["basis"] for r in rd.hint_reads(h["id"])} == {"index"}
    ticks(h["id"], 61)
    rd.read_hint(c, h["id"], "interval", now_at("15:29"))
    p = worker[-1]
    assert p["symbol"] == "NIFTY26NOV25800CE" and len(p["candles"]) == 61 and p["levels"] == [150.0, 230.0, 185.0]
    assert p["candles"][1] == {"at": f"{DAYS[-1]} 09:16", "o": 180.1, "h": 180.15, "l": 180.1, "c": 180.15, "v": 0}      # 1-minute OHLC from the 5 s ticks
    last = rd.hint_reads(h["id"])[-1]
    assert last["paths_json"]["basis"] == "premium" and last["hint_id"] == h["id"] and last["paths_json"]["levels"] == {"stop": 150.0, "t1": 230.0, "zone_top": 185.0}


def test_touch_probabilities_from_paths():
    last = 200.0
    closes = [[201, 205, 232], [199, 160, 150], [200, 200, 200], [190, 148, 240]]
    highs = [[202, 206, 233], [200, 170, 160], [201, 201, 201], [191, 150, 241]]
    lows = [[199, 203, 230], [195, 149, 140], [199, 199, 199], [185, 147, 238]]
    r = pth.summarise(last, closes, highs, lows, [150.0, 230.0], [2, 3])
    r3 = next(x for x in r if x["horizon"] == 3)
    assert r3["touch"] == {"150": 0.5, "230": 0.25}                         # first crossed: 230 / 150 / none / 150
    assert r3["touch_any"] == {"150": 0.5, "230": 0.5}                      # path 4 touches 230 after its stop
    assert r3["p_up"] == 0.5 and r3["paths_summary"]["q50"][0] == 199.5
    r2 = next(x for x in r if x["horizon"] == 2)
    assert r2["touch"] == {"150": 0.5, "230": 0.0} and r2["p_up"] == 0.25
    assert pth.first_touch(200, [231, 240], [149, 100], [150.0, 230.0], 2) == 230.0   # both in one minute: the nearer (230) counts


# --- scoring, the scorecard and the gate ----------------------------------------------------------------------------------------

def put_read(sym="NIFTY", at_="2026-11-19T10:00:00+05:30", h=30, p_up=0.64, right=None, last=25800.0, touch=None, basis="index", hint_id=None):
    with get_db() as conn:
        rid = conn.execute("INSERT INTO market_reads (at, symbol, interval, horizon, p_up, exp_range, touch_json, paths_json, version, ms, hint_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (at_, sym, "1m", h, p_up, 20.0, json.dumps(touch or {}), json.dumps({"last": last, "basis": basis, "index": sym if basis == "index" else "NIFTY"}),
                            rd.VERSION, 400, hint_id)).lastrowid
        if right is not None:
            conn.execute("UPDATE market_reads SET scored_at = ?, outcome_json = ? WHERE id = ?", ("x", json.dumps({"direction_right": right, "touch_right": None}), rid))
    return rid


def test_scoring_direction_and_touch_outcomes():
    bars = seed_index()
    at10 = next(b for b in bars if b["d"] == DAYS[-1] and b["t"] == "10:00")
    r_up = put_read(at_=f"{DAYS[-1]}T10:00:00+05:30", p_up=0.7, last=at10["c"])
    r_dn = put_read(at_=f"{DAYS[-1]}T10:00:00+05:30", p_up=0.3, last=at10["c"], h=15)
    lvl_hi, lvl_lo = at10["c"] + 2.0, at10["c"] - 50.0
    r_t = put_read(at_=f"{DAYS[-1]}T10:00:00+05:30", p_up=0.6, last=at10["c"], touch={pth.level_key(lvl_hi): 0.6, pth.level_key(lvl_lo): 0.1})
    r_wait = put_read(at_=f"{DAYS[-1]}T15:20:00+05:30", h=30, last=at10["c"])
    out = rd.score(now_at("18:40"))
    assert out == {"scored": 3, "missing": 0, "waiting": 1}
    o = {r["id"]: r["outcome_json"] for r in rd.rows()}
    assert o[r_up]["up"] is True and o[r_up]["direction_right"] is True and o[r_dn]["direction_right"] is False
    assert o[r_t]["first"] == pth.level_key(lvl_hi) and o[r_t]["touch_right"] is True
    end = next(b for b in bars if b["d"] == DAYS[-1] and b["t"] == "10:30")
    assert o[r_up]["close"] == round(end["c"], 2) and o[r_wait] is None
    assert rd.score(now_at("18:40") + timedelta(days=1))["missing"] == 1      # never reached its horizon: scored missing after a day


def test_scorecard_numbers_and_the_cards_gate_on_and_off():
    c = cfg(kronos_show="cards")
    for i in range(100):
        put_read("NIFTY", p_up=0.7, right=i < 58)
        put_read("BANKNIFTY", p_up=0.4, right=i < 56)
    for i in range(10):
        put_read("SENSEX", p_up=0.55, right=i < 4)
    put_read("NIFTY26NOV25800CE", h=15, p_up=0.62, right=True, basis="premium", hint_id=7)
    sc = rd.scorecard()
    n = rd.card_row(sc, "NIFTY", 30)
    assert n == {"name": "NIFTY", "interval": "1m", "horizon": 30, "reads": 100, "scored": 100, "direction_pct": 58.0, "touch_n": 0,
                 "touch_pct": None, "avg_conf": 70.0, "since": "2026-11-19"}
    assert rd.card_row(sc, "BANKNIFTY", 30)["avg_conf"] == 60.0 and rd.card_row(sc, "SENSEX", 30)["direction_pct"] == 40.0
    assert [x["name"] for x in sc] == ["NIFTY", "BANKNIFTY", "SENSEX", "option premium"]
    g = rd.gate(c, sc)
    assert g["cards"] is True
    assert rd.chip(c, "NIFTY", 30, sc) == "candle model · cards · hit rate 58 % over 100 reads"
    assert rd.gate(cfg(), sc)["cards"] is False and rd.chip(cfg(), "NIFTY", 30, sc).startswith("candle model · shadow · hit rate 58 %")
    put_read("BANKNIFTY", p_up=0.4, right=False)
    for _ in range(5):
        put_read("BANKNIFTY", p_up=0.4, right=False)
    g2 = rd.gate(c)
    assert g2["cards"] is False and "BANKNIFTY 30 min direction 52.8 % (needs 55 %)" in g2["why"]


def test_shadow_shows_the_chip_only_and_cards_show_the_model(worker):
    seed_index()
    c = cfg()
    rd.read_index(c, "NIFTY", now_at("15:29"))
    v = mv.panel(c, "NIFTY", now_at("15:29"))
    assert v["mode"] == "shadow" and v["tiles"]["model"]["state"] == "shadow" and "64" not in json.dumps(v["tiles"]["model"])
    assert "model 64" not in v["backdrop"]["numbers"] and v["chip"] == "candle model · shadow · not scored yet"
    for i in range(100):
        put_read("NIFTY", p_up=0.7, right=i < 60)
        put_read("BANKNIFTY", p_up=0.7, right=i < 60)
    v2 = mv.panel(cfg(kronos_show="cards"), "NIFTY", now_at("15:29"))
    assert v2["tiles"]["model"]["head"] == "64 % up" and "model 64 % up · kronos-mini-v1" in v2["backdrop"]["numbers"]
    assert v2["tiles"]["trend"]["head"] == "up · above VWAP" and v2["tiles"]["levels"]["head"]


# --- words ---------------------------------------------------------------------------------------------------------------------

def test_card_words_four_lines_bad_figure_dropped_verdict_by_code(worker, monkeypatch):
    seed_index()
    c = cfg()
    c.ai.enabled = True
    queued = []
    monkeypatch.setattr(mw, "enqueue", lambda config, rid, key, note: queued.append(key))
    h = hint()
    rid = mw.card(c, h["id"], "ingest", now_at("15:29"))
    row = ai4.load(rid)
    src = row["input_json"]["text"]
    assert row["kind"] == "market" and row["prompt_v"] == "market-v1" and row["input_json"]["verdict"] == "supports the call"
    assert "Candle model" not in src                                                   # shadow: the model's numbers never reach the words
    tr = mv.index_facts("NIFTY", now_at("15:29"))["trend"]
    reply = (f"trend: NIFTY is up and above VWAP {mv.fmt(tr['vwap'])}.\nlevels: resistance is 99,999 points away.\n"
             "stop: The stop 150 is far from the price.\nverdict: The trend agrees. against the call\nextra chatter line")
    mw.run_row(c, rid, generate=lambda s, u: reply, now=now_at("15:30"))
    lines = ai4.load(rid)["lines_json"]["lines"]
    assert [x["label"] for x in lines] == ["trend", "stop", "verdict"]                    # 99,999 is not in the input: dropped
    assert lines[-1]["text"] == "The trend agrees; supports the call"                      # the model's ending replaced by code
    assert mw.card(c, h["id"], "ingest", now_at("15:31")) is None                          # once per stage
    assert mw.card(c, h["id"], "fill", now_at("15:31")) is not None                        # refreshed once on the fill
    assert queued[0] == f"market:card:{h['id']}:ingest" and f"market:entry:{h['id']}" in queued
    v = mv.card(c, bk.load(h["id"]), now_at("15:31"))
    assert v["verdict"] == "supports the call" and v["tiles"]["index"]["head"] == "with the call"


def test_verdict_by_code():
    assert mv.verdict("up", "up", None, False) == "supports the call"
    assert mv.verdict("down", "up", 0.9, False) == "against the call"
    assert mv.verdict("up", "up", 0.40, True) == "mixed"                                   # trend with, model against
    assert mv.verdict("up", "up", 0.50, True) == "supports the call"                       # the model abstains between 45 and 55 %
    assert mv.verdict("flat", "down", 0.30, True) == "supports the call" and mv.verdict("flat", "down", 0.5, True) == "mixed"
    assert mv.call_dir({"side": "BUY", "opt_type": "PE"}) == "down" and mv.call_dir({"side": "SELL", "opt_type": "PE"}) == "up"


def test_entry_timing_only_for_unfilled_zone_entries(worker):
    seed_index()
    c = cfg()
    zone = hint()
    ticks(zone["id"], 70, 190.0, 0.01)
    rd.read_hint(c, zone["id"], "ingest", now_at("15:29"))
    v = mv.card(c, bk.load(zone["id"]), now_at("15:29"))
    assert v["entry"] and v["entry"]["zone"] == "zone 180–185" and v["entry"]["code"].startswith("zone top 185")      # shadow: code only
    for i in range(100):
        put_read("NIFTY", p_up=0.7, right=i < 60)
        put_read("BANKNIFTY", p_up=0.7, right=i < 60)
    v2 = mv.card(cfg(kronos_show="cards"), bk.load(zone["id"]), now_at("15:29"))
    assert v2["entry"]["code"] == "pullback into the zone likely: 57 % of paths touch the zone top 185 in 15 min"
    assert v2["tiles"]["model"]["sub"].startswith("stop level touched first: 20 % · T1 first: 20 %") and v2["fan"]["stop"] == 150.0
    filled = hint(kite={"state": "entered", "entry_px": 184.0, "entry_at": "2026-11-19T10:10:00+05:30"})
    assert mv.card(c, bk.load(filled["id"]), now_at("15:29"))["entry"] is None
    market = hint(entry={"mode": "market"})
    assert mv.card(c, bk.load(market["id"]), now_at("15:29"))["entry"] is None


# --- the grade ---------------------------------------------------------------------------------------------------------------------

def test_grade_steps_with_the_market_only_when_cards(monkeypatch):
    seed_index()
    monkeypatch.setattr(ai4, "grade_of", lambda rec, shp, words: ("C", "forced"))
    h = hint()
    d = ai4.grade_input(h, now_at("15:29"), cfg())
    assert d["grade"] == "C" and d["market"]["align"] == "with" and d["market"]["applied"] is False      # shadow: stored, unchanged
    assert "Market (code): NIFTY up · above VWAP · supports the call · not counted in the grade" in d["text"]
    for i in range(100):
        put_read("NIFTY", p_up=0.5, right=i < 60)
        put_read("BANKNIFTY", p_up=0.5, right=i < 60)
    d2 = ai4.grade_input(h, now_at("15:29"), cfg(kronos_show="cards"))
    assert d2["grade"] == "B" and d2["why"] == "forced · one step up: with the market"
    pe = hint(typ="PE")
    assert ai4.grade_input(pe, now_at("15:29"), cfg(kronos_show="cards"))["grade"] == "D"                 # against: one step down
    monkeypatch.setattr(ai4, "grade_of", lambda rec, shp, words: ("–", "too few"))
    assert ai4.grade_input(h, now_at("15:29"), cfg(kronos_show="cards"))["grade"] == "–"                  # never on "–"


# --- the speed guarantee -------------------------------------------------------------------------------------------------------------

def test_arm_and_place_entry_never_wait_on_a_5_s_worker(pushed, monkeypatch):  # noqa: F811
    for i, d in enumerate(DAYS):                                                 # SENSEX candles, so the hint read really calls the worker
        cd.store(cd.TOKENS["SENSEX"], d, [{k: x[k] for k in ("t", "o", "h", "l", "c", "v")} for x in day_bars(82000 + i * 50, 0.1, 375, 0.2, d)])
    calls = []

    def slow(payload):
        calls.append((threading.current_thread().name, _t.monotonic()))
        _t.sleep(5)
        return fake_result(payload)
    monkeypatch.setattr(rd, "WORKER", slow)
    seen = []

    def spy(config, hid, why):
        seen.append((hid, why, [r["leg"] for r in ko.rows_for(hid)], _t.monotonic()))
        return REAL_QUEUE(config, hid, why)
    monkeypatch.setattr(rd, "queue_hint", spy)
    c, w, s = auto_setup()
    w.prices[S82300] = 309.0
    t0 = _t.monotonic()
    r = post(c, w, s, 1, "13:52", CALL2)                        # ingest → plan → auto arm → place_entry → the read queued
    took = _t.monotonic() - t0
    hid = r["hint_id"]
    assert bk.load(hid)["kite"]["state"] == "armed" and took < 1.0
    assert seen and seen[0][:3] == (hid, "ingest", ["entry"])                    # queued after the order row exists
    assert rd.hint_reads(hid) == [] and all(name != "MainThread" for name, _ in calls)
    t1 = _t.monotonic()
    h2 = post(c, w, s, 2, "13:54", "BUY SENSEX 82200 CE ONLY ABOVE 600\nSL 480\nTGT 700")
    assert _t.monotonic() - t1 < 1.0 and h2["hint_id"]
    t2 = _t.monotonic()
    c.tips.kite_auto_max_open = 5
    out = ko.place_entry(c, s, h2["hint_id"], lots=1, by="click", now=at("13:55", D19))
    assert _t.monotonic() - t2 < 1.0 and out["kite"]["state"] in ("armed", "entered")
    rd.drain(15)
    rows_ = rd.hint_reads(hid)
    assert rows_ and rows_[0]["version"] == "kronos-mini-v1"                     # the read landed later, on its own thread
    assert calls[0][1] >= seen[0][3] and calls[0][0] == "market-hint-reads"     # the worker was called after the queue, off the order path


def test_the_index_loop_skips_a_tick_while_a_pass_runs(monkeypatch):
    seed_index()
    gate_ = threading.Event()

    def slow(payload):
        gate_.wait(5)
        return fake_result(payload)
    monkeypatch.setattr(rd, "WORKER", slow)
    w = HistClient(has_token=False)
    svc = rd.MarketService(SimpleNamespace(config=cfg(), client=w))
    assert svc.tick(now_at("10:04"))["why"] is None
    assert svc.tick(now_at("10:05"))["why"] == "busy" and svc.skipped == 1      # never queues up
    gate_.set()
    for _ in range(100):
        if svc._busy.acquire(blocking=False):
            svc._busy.release()
            break
        _t.sleep(0.05)
    assert svc.tick(now_at("10:06"))["why"] is None
    gate_.set()


# --- the switches and the login ------------------------------------------------------------------------------------------------------

class HistClient:
    def __init__(self, has_token=True):
        self.has_token = has_token
        self.calls = []

    def historical_minute(self, token, start, end, label=""):
        self.calls.append(("hist", label, start.isoformat(), end.isoformat()))
        out = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                for i in range(30):
                    m = 9 * 60 + 15 + i
                    out.append({"date": datetime(d.year, d.month, d.day, m // 60, m % 60, tzinfo=IST), "open": 100 + i, "high": 101 + i, "low": 99 + i,
                                "close": 100.5 + i, "volume": 0})
            d += timedelta(days=1)
        return out

    def quote(self, keys):
        self.calls.append(("quote", tuple(keys)))
        return {cd.VIX_KEY: {"last_price": 17.33, "ohlc": {"close": 16.1}}}


def test_kronos_off_fetches_nothing_and_the_panel_says_off(worker):
    w = HistClient()
    c = cfg(kronos=False)
    svc = rd.MarketService(SimpleNamespace(config=c, client=w))
    assert svc.tick(now_at("10:04"), wait=True) == {"why": "off"} and w.calls == [] and worker == []
    v = mv.panel(c, "NIFTY", now_at("10:04"), client=w)
    assert v["on"] is False and v["off"].startswith("the candle model is off") and v["tiles"] is None
    assert v["chip"] == "candle model · off"
    assert REAL_QUEUE(c, 1, "ingest") is False


def test_logged_out_fetches_nothing_and_says_candles_need_the_kite_login(worker):
    w = HistClient(has_token=False)
    c = cfg()
    svc = rd.MarketService(SimpleNamespace(config=c, client=w))
    svc.tick(now_at("10:04"), wait=True)
    assert w.calls == [] and rd.status()["why"] == "candles need the Kite login"
    v = mv.panel(c, "NIFTY", now_at("10:04"), client=w)
    assert v["login"] == "candles need the Kite login" and v["empty"] == "candles need the Kite login"


def test_logged_in_fetches_two_sessions_once_then_today_and_never_deletes(worker):
    w = HistClient()
    c = cfg()
    svc = rd.MarketService(SimpleNamespace(config=c, client=w))
    svc.tick(now_at("10:04"), wait=True)
    hist = [x for x in w.calls if x[0] == "hist"]
    assert [x[1] for x in hist] == ["NIFTY", "BANKNIFTY", "SENSEX"] and hist[0][2] == "2026-11-17" and hist[0][3] == "2026-11-19"
    assert rd.vix()["last"] == 17.33
    svc.tick(now_at("10:05"), wait=True)
    assert [x for x in w.calls if x[0] == "hist"][-1][2] == "2026-11-19"          # the stored sessions are not asked again
    assert cd.stored_days(cd.TOKENS["NIFTY"]) == ["2026-11-17", "2026-11-18", "2026-11-19"]
    cd.store(cd.TOKENS["NIFTY"], "2026-11-19", [{"t": "15:00", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 0}])
    assert len(cd.load(cd.TOKENS["NIFTY"], 1)) == 31                               # a fetch merges, never drops a stored minute
    assert len(worker) == 6                                                         # one read per index per pass (two passes)


# --- storage: the archive move and the tips_ai migration -------------------------------------------------------------------------------

def test_market_reads_and_market_words_are_moved_by_the_archive_never_deleted():
    c = AppConfig()
    old = (datetime.now(IST) - timedelta(days=200)).isoformat(timespec="seconds")
    new = datetime.now(IST).isoformat(timespec="seconds")
    r_old, r_new = put_read(at_=old), put_read(at_=new)
    w_old = mw.insert("panel", None, {"text": "x", "symbol": "NIFTY"}, datetime.now(IST) - timedelta(days=200))
    with sqlite3.connect(str(archive.archive_path())) as a:              # an archive made before 'market' joined the CHECK
        a.execute("CREATE TABLE tips_ai (id INTEGER PRIMARY KEY AUTOINCREMENT, hint_id INTEGER NULL, kind TEXT NOT NULL CHECK(kind IN "
                  "('review', 'grade', 'preflight', 'weekly')), day TEXT NOT NULL, at TEXT NOT NULL, model TEXT, prompt_v TEXT, input_json TEXT NOT NULL, "
                  "lines_json TEXT, ok INTEGER NULL, ms INTEGER)")
        a.execute("INSERT INTO tips_ai (id, kind, day, at, input_json) VALUES (99999, 'review', '2026-01-01', '2026-01-01T10:00:00', '{}')")
    out = archive.run(c)
    assert out["moved_by_table"]["market_reads"] == 1 and out["moved_by_table"]["tips_ai"] == 1
    assert [r["id"] for r in rd.rows()] == [r_new]
    with sqlite3.connect(str(archive.archive_path())) as a:
        assert [r[0] for r in a.execute("SELECT id FROM market_reads")] == [r_old]
        assert sorted(r[0] for r in a.execute("SELECT id FROM tips_ai")) == sorted([w_old, 99999])       # the old archive row kept


def test_tips_ai_gains_kind_market_and_keeps_every_row(tmp_path):
    p = tmp_path / "old.db"
    with sqlite3.connect(str(p)) as conn:
        conn.execute("CREATE TABLE tips_ai (id INTEGER PRIMARY KEY AUTOINCREMENT, hint_id INTEGER NULL, kind TEXT NOT NULL CHECK(kind IN "
                     "('review', 'grade', 'preflight', 'weekly')), day TEXT NOT NULL, at TEXT NOT NULL, model TEXT, prompt_v TEXT, input_json TEXT NOT NULL, "
                     "lines_json TEXT, ok INTEGER NULL, ms INTEGER)")
        conn.execute("INSERT INTO tips_ai (id, hint_id, kind, day, at, input_json, ok) VALUES (7, 3, 'grade', '2026-11-19', '2026-11-19T10:00:00', '{\"grade\": \"B\"}', 1)")
    init_db(str(p))
    init_db(str(p))                                                       # runs once
    with sqlite3.connect(str(p)) as conn:
        assert conn.execute("SELECT id, hint_id, kind, input_json, ok FROM tips_ai").fetchall() == [(7, 3, "grade", '{"grade": "B"}', 1)]
        conn.execute("INSERT INTO tips_ai (hint_id, kind, day, at, input_json) VALUES (3, 'market', 'd', 'a', '{}')")
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name = 'tips_ai_pre_market'").fetchall()


# --- settings, the scheduler, the README, the source rules -----------------------------------------------------------------------------

def test_settings_defaults_and_checks():
    c = AppConfig()
    m = c.market
    assert (m.kronos, m.kronos_show, m.interval_s, m.horizon_min, m.samples, m.port) == (True, "shadow", 60, 30, 10, 8765)
    for bad in ({"kronos_show": "always"}, {"interval_s": 10}, {"horizon_min": 20}, {"samples": 0}):
        with pytest.raises(ValueError):
            c.update_section("market", bad)
    assert c.market.kronos_show == "shadow" and c.market.interval_s == 60
    c.update_section("market", {"kronos_show": "cards", "interval_s": "30"})
    assert c.market.kronos_show == "cards" and c.market.interval_s == 30


def test_scoring_job_is_due_at_6_40_pm_on_trading_days():
    c = AppConfig()
    assert rd.due(now_at("18:39"), c, set(), True) == [] and rd.due(now_at("18:40"), c, set(), True) == ["market_score"]
    assert rd.due(now_at("18:40"), c, {"market_score"}, True) == [] and rd.due(now_at("18:40"), c, set(), False) == []


def test_routes_answer(worker):
    from fastapi.testclient import TestClient
    from backend.api import routes
    from backend.api.routes import router
    from fastapi import FastAPI
    seed_index()
    app = FastAPI()
    app.include_router(router)
    routes.set_state(routes.AppState(cfg(), HistClient(has_token=False)))
    try:
        tc = TestClient(app)
        a = tc.get("/api/market/now?index=BANKNIFTY").json()
        assert a["symbol"] == "BANKNIFTY" and a["login"] == "candles need the Kite login"
        b = tc.get("/api/market/scorecard").json()
        assert b["rows"] == [] and b["gate"]["mode"] == "shadow" and b["settings"]["kronos_show"] == "shadow"
    finally:
        routes.set_state(None)


def test_the_order_path_source_never_calls_the_worker():
    src = (ROOT / "backend/tips/kite_orders.py").read_text() + (ROOT / "backend/tips/kite_auto.py").read_text()
    assert "call_worker" not in src and "read_hint" not in src and "drain(" not in src
    assert src.count("mr.queue_hint(") == 1                                      # the fill: put on the queue, nothing else
    words_src = (ROOT / "backend/market/words.py").read_text()
    assert "json_mode=False" in words_src and 'PROMPT_V = "market-v1"' in words_src


def test_readme_has_the_market_read_section():
    t = (ROOT / "README.md").read_text()
    s = t[t.index("## 📈 Market read"):]
    for w in ("chart.py", "Kronos-mini", "shadow", "kronos_show", "interval_s", "arm", "aarch64", "[AI]", "6:40 PM", "data/ai/kronos"):
        assert w in s, w


# --- the real engine (only where torch and the weights are) ---------------------------------------------------------------------------

def _weights_present() -> bool:
    if importlib.util.find_spec("torch") is None:
        return False
    if (ROOT / "data/ai/kronos/Kronos-mini/config.json").exists():
        return True
    try:
        from huggingface_hub import snapshot_download
        snapshot_download("NeoQuasar/Kronos-mini", local_files_only=True)
        snapshot_download("NeoQuasar/Kronos-Tokenizer-2k", local_files_only=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _weights_present(), reason="torch or the Kronos-mini weights are not on this machine")
def test_real_worker_one_read_on_synthetic_candles(monkeypatch):
    import os
    import subprocess
    import sys
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = {**os.environ, "BP_KRONOS_DIR": str(ROOT / "data/ai/kronos")}
    p = subprocess.Popen([sys.executable, "-m", "backend.market.kronos_worker", "--port", str(port)], cwd=str(ROOT), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        st = {}
        for _ in range(240):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                    st = json.loads(r.read())
                if st.get("state") in ("ready", "error"):
                    break
            except OSError:
                pass
            _t.sleep(0.5)
        assert st.get("state") == "ready", st
        bars = seed_index()
        c = cfg(port=port)
        monkeypatch.setattr(rd, "WORKER_TIMEOUT_S", 60.0)
        res = rd.call_worker(c, {"symbol": "NIFTY", "interval": "1m", "candles": cd.to_worker(bars), "horizon": 30, "horizons": [15, 30],
                                 "samples": 10, "levels": [bars[-1]["c"] + 20]}, timeout=60)
        assert res and res["version"] == "kronos-mini-v1" and 0 <= res["p_up"] <= 1 and len(res["paths_summary"]["q50"]) == 30
        assert [x["horizon"] for x in res["reads"]] == [15, 30] and set(res["touch"]) == {pth.level_key(bars[-1]["c"] + 20)}
    finally:
        p.terminate()
        p.wait(10)
