"""₿ The feed (backend/crypto/feed.py) with a fake Delta Exchange: parse, upsert, closed-candle detection, backfill paging,
the rate limit, lag health and the Home item, the service pass that hands each newly closed candle to the signals."""

from types import SimpleNamespace

import pytest

from backend.crypto import feed
from backend.crypto import signals as sg
from backend.engine import attention
from tests.crypto_world import DAY1, T_DAY, FakeDelta, cfg, k


def test_parse_newest_first_and_bad_rows():
    body = {"result": [{"time": 600, "open": 3, "high": 4, "low": 2, "close": 3.5, "volume": 7},
                       {"time": 300, "open": "1", "high": 2, "low": 0.5, "close": 1.5, "volume": None},
                       {"time": 0, "open": None}]}
    assert feed.parse(body) == [k(300, 1, 2, 0.5, 1.5, 0), k(600, 3, 4, 2, 3.5, 7)]
    with pytest.raises(feed.FeedError):
        feed.parse({"error": "bad symbol"})


def test_upsert_updates_the_open_candle_and_never_deletes():
    feed.upsert("BTCUSD", "5m", [k(0, 1, 2, 0, 1), k(300, 1, 2, 0, 1.5)])
    feed.upsert("BTCUSD", "5m", [k(300, 1, 3, 0, 2.5)])
    assert feed.load("BTCUSD", "5m") == [k(0, 1, 2, 0, 1), k(300, 1, 3, 0, 2.5)]
    assert feed.load("BTCUSD", "5m", limit=1) == [k(300, 1, 3, 0, 2.5)]
    assert feed.load("BTCUSD", "1m") == []


def test_closed_candles_only():
    cs = [k(0, 1, 1, 1, 1), k(300, 1, 1, 1, 1), k(600, 1, 1, 1, 1)]
    assert [b["t"] for b in feed.closed(cs, "5m", 900)] == [0, 300, 600]
    assert [b["t"] for b in feed.closed(cs, "5m", 899)] == [0, 300]          # the 600 candle is still forming


def test_backfill_pages_and_resumes():
    now = 3 * feed.PAGE * 300 + 1000
    series = {"5m": [k(t, 1, 2, 0, 1) for t in range(0, now, 300)]}
    fake = FakeDelta(series)
    sleeps = []
    cl = feed.DeltaClient(get=fake, sleep=sleeps.append)
    r = feed.backfill(cl, "BTCUSD", "5m", 21, now)                                      # 21 days: 3 full pages + the rest
    assert r["calls"] == 4 and feed.bounds("BTCUSD", "5m")[2] == len(series["5m"])
    assert all(c["resolution"] == "5m" and c["symbol"] == "BTCUSD" for c in fake.calls)
    assert all(c["end"] - c["start"] <= feed.PAGE * 300 for c in fake.calls)
    fake.calls.clear()
    feed.backfill(cl, "BTCUSD", "5m", 21, now + 600)                                    # only from the newest stored candle
    assert len(fake.calls) == 1 and fake.calls[0]["start"] == series["5m"][-1]["t"]
    assert all(0 < s <= feed.MIN_GAP_S for s in sleeps)                                   # ≤ 2 calls a second


def test_health_lag_and_home_item():
    c = cfg()
    feed.upsert("BTCUSD", "5m", [k(T_DAY - 300 * i, 1, 1, 1, 1) for i in range(5)])
    h = feed.health("BTCUSD", "5m", T_DAY + 300)
    assert h["last_t"] == T_DAY and h["lag_candles"] == 0 and h["last_label"] == "10:00 AM" and not h["behind"]
    h = feed.health("BTCUSD", "5m", T_DAY + 300 * 6)
    assert h["lag_candles"] == 5 and h["behind"]
    assert feed.attention_items(c, T_DAY + 300 * 6)[0][1].startswith("₿ BTC feed 5 candles behind · last candle 10:00 AM")
    assert feed.attention_items(c, T_DAY + 300 * 2) == []
    c.crypto.enabled = False
    assert feed.attention_items(c, T_DAY + 300 * 6) == []
    out = attention.signals(cfg(), False)
    assert "crypto" in out


def test_service_tick_processes_each_closed_candle_once(monkeypatch):
    c = cfg(history_days=1)
    now = T_DAY + 303
    series = {"5m": [k(t, 59000, 59010, 58990, 59000) for t in range(T_DAY - 2 * 86400, T_DAY + 301, 300)],
              "1d": [k(DAY1 - 86400 * i, 1, 2, 0, 1) for i in range(5)]}
    fake = FakeDelta(series)
    seen = []
    monkeypatch.setattr(sg, "process", lambda config, t, now_ts=None, candles=None: seen.append(t) or [])
    svc = feed.CryptoService(SimpleNamespace(config=c), client=feed.DeltaClient(get=fake, sleep=lambda s: None), clock=lambda: now)
    out = svc.tick()
    assert out["processed"] == [T_DAY] and seen == [T_DAY]         # the first start: the last closed candle only
    assert svc.tick()["processed"] == []                             # nothing new
    series["5m"].append(k(T_DAY + 300, 59000, 59010, 58990, 59000))
    svc.clock = lambda: T_DAY + 603
    assert svc.tick()["processed"] == [T_DAY + 300]
    assert any(cl["resolution"] == "1d" for cl in fake.calls)
    assert 1 <= svc.sleep_s(T_DAY + 100) <= 30 and svc.sleep_s(T_DAY + 290) == pytest.approx(13)
    fake.fail = 1                                                    # a failing call: counted, the loop goes on
    svc.clock = lambda: T_DAY + 903
    with pytest.raises(feed.FeedError):
        svc.tick()
    svc._error(feed.FeedError("HTTP 502"))
    assert svc.errors == 1 and "502" in feed.saved_health()["last_error"]
