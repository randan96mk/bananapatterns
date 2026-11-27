"""₿ Detections and alerts (backend/crypto/signals.py, read.py, snapshot.py) with a fake Telegram and a fake text model:
sent inside the alert hours, quiet outside (state quiet, the digest at alert_from), the Telegram line, the snapshot
file, the [AI] line appended or sent second (never gating), the forming heads-up (off by default; on: provisional then
confirmed / cancelled), the hand quiet ("btc quiet 2h")."""

import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.config import IST, db_path
from backend.crypto import feed
from backend.crypto import read
from backend.crypto import signals as sg
from tests.crypto_world import T_DAY, T_NIGHT, cfg, hammer_world, k, telegram

LINE = "₿ hammer on S1 59,000 · 5 min · 10:00 AM · long bias · stop under 59,055 · 1R 59,155 · EMA 20 above ✓ · volume 1.28×"
QUIET = dict(vol_len=20, dragonfly_doji="off", tweezer_bottom="off")


def _hammer(c, t=T_DAY):
    hammer_world(t)
    sg.process(c, t, now_ts=t + 303)
    return sg.rows("pattern = 'hammer'")[0]


def test_sent_inside_the_hours_with_the_line_and_the_snapshot(monkeypatch):
    tg = telegram(monkeypatch)
    c = cfg(**QUIET)
    r = _hammer(c)
    assert r["state"] == "sent" and r["sent_at"] and r["why"] is None
    assert (r["level_name"], r["level_px"], r["entry"], r["stop"], r["r1"]) == ("S1", 59000, 59105, 59055, 59155)
    assert r["facts_json"]["capped"] is True                       # 58,990 − 10 is 125 under the entry: capped at 50
    assert tg.sent == [("photo", LINE)]
    assert sg.line(r) == LINE
    png = tg.photos[0]
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w = int.from_bytes(png[16:20], "big")
    assert 600 <= w <= 900
    p = sg.snaps_dir() / f"{r['id']}.png"
    assert p.is_file() and r["png_path"] == str(p)
    assert sg.snaps_dir() == db_path().parent / "crypto" / "snaps"          # data/crypto/snaps next to the DB (git-ignored)


def test_mark_only_and_dropped_rows_never_on_telegram(monkeypatch):
    tg = telegram(monkeypatch)
    c = cfg(vol_len=20, hammer="mark")
    _hammer(c)
    states = {r["pattern"]: r["state"] for r in sg.rows()}
    assert states["hammer"] == "mark" and states["dragonfly_doji"] == "mark" and states["tweezer_bottom"] == "mark"
    assert tg.sent == []
    c2 = cfg(vol_len=20, ema_len=20)
    hammer_world(T_DAY + 3600)
    feed.upsert("BTCUSD", "5m", [k(T_DAY + 3600, 59090, 59105, 58990, 59000, 130)])     # a red close under the EMA: not a hammer + EMA fails
    sg.process(c2, T_DAY + 3600, now_ts=T_DAY + 3903)
    assert tg.sent == []


def test_quiet_outside_the_hours_then_the_digest(monkeypatch):
    tg = telegram(monkeypatch)
    c = cfg(**QUIET)
    r = _hammer(c, T_NIGHT)
    assert r["state"] == "quiet" and r["sent_at"] is None and tg.sent == []
    assert sg.digest_if_due(c, now_ts=T_NIGHT + 600) == 0          # still quiet hours (2:20 AM)
    eight = datetime(2026, 11, 24, 8, 5, tzinfo=IST).timestamp()
    assert sg.digest_if_due(c, now_ts=eight) == 1
    head, body = tg.sent[-1][1].split("\n", 1)
    assert head == "₿ quiet hours · 1 detection" and body.startswith("• ₿ hammer on S1 59,000 · 5 min · 2:10 AM · long bias")
    assert sg.load(r["id"])["digest_at"] and sg.digest_if_due(c, now_ts=eight + 60) == 0
    c.crypto.digest = False
    assert sg.digest_if_due(c, now_ts=eight) == 0


def test_hours_over_midnight_and_days():
    c = cfg(alert_from="22:00", alert_until="06:00")
    assert sg.in_hours(c, datetime(2026, 11, 24, 23, 0, tzinfo=IST)) and sg.in_hours(c, datetime(2026, 11, 24, 5, 0, tzinfo=IST))
    assert not sg.in_hours(c, datetime(2026, 11, 24, 12, 0, tzinfo=IST))
    c = cfg(alert_days=["mon"])
    assert not sg.in_hours(c, datetime(2026, 11, 24, 12, 0, tzinfo=IST))            # a Tuesday
    assert sg.hours_text(cfg(), datetime(2026, 11, 24, 12, 0, tzinfo=IST)) == "alerts 8:00 AM – 11:00 PM"


def test_btc_quiet_command(monkeypatch):
    from backend.alerts.commands import BotCommands
    now = datetime(2026, 11, 24, 14, 10, tzinfo=IST)
    c = cfg()
    bot = BotCommands(SimpleNamespace(config=c), clock=lambda: now)
    assert bot.btc(["quiet", "2h"]) == "₿ BTC alerts quiet until 4:10 PM · detections are still stored and listed in the digest"
    assert sg.quiet_until(now).isoformat().startswith("2026-11-24T16:10")
    assert not sg.in_hours(c, now) and sg.hours_text(c, now) == "alerts 8:00 AM – 11:00 PM · quiet until 4:10 PM"
    assert bot.btc(["quiet", "30m"]).endswith("detections are still stored and listed in the digest")
    assert bot.btc(["quiet", "off"]).startswith("₿ BTC alerts resumed")
    assert sg.quiet_until(now) is None and sg.in_hours(c, now)
    assert bot.btc(["quiet", "soon"]).startswith("₿ use:")
    assert bot.btc([]).startswith("₿ BTC patterns on · BTCUSD 5m")
    # through the poller: the words "btc quiet 2h" from the owner's chat
    tg = SimpleNamespace(sent=[], configured=True, get_updates=lambda off, to: [{"update_id": 1, "message": {"chat": {"id": 42}, "date": now.timestamp(), "text": "btc quiet 2h"}}],
                         send_message=lambda chat, html, *a, **kw: tg.sent.append(html))
    from backend.alerts.dispatch import Notifier
    assert bot.poll_once(Notifier(c.alerts, tg)) == 1 and "quiet until 4:10 PM" in tg.sent[-1]


def _ai_on(monkeypatch, gen):
    from backend.ai import guard
    monkeypatch.setattr(guard, "allowed", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(read, "GENERATE", gen)


def test_ai_line_appended_when_ready(monkeypatch):
    tg = telegram(monkeypatch)
    _ai_on(monkeypatch, lambda system, user: "read: a clean rejection of S1 59,000 with the close above EMA 20.")
    c = cfg(**QUIET)
    c.ai.enabled = True
    r = _hammer(c)
    assert tg.sent[0][1] == LINE + "\n[AI] a clean rejection of S1 59,000 with the close above EMA 20."
    aj = sg.load(r["id"])["ai_json"]
    assert aj["line"].startswith("a clean rejection") and aj["verdict"] == "supports the long" and aj["inline"] is True
    assert "59,000" in aj["facts"]


def test_ai_line_sent_second_and_never_gates(monkeypatch):
    tg = telegram(monkeypatch)
    monkeypatch.setattr(sg, "AI_WAIT_S", 0.2)

    def slow(system, user):
        time.sleep(0.8)
        return "read: S1 59,000 held on the close."
    _ai_on(monkeypatch, slow)
    c = cfg(**QUIET)
    c.ai.enabled = True
    r = _hammer(c)
    assert tg.sent[0] == ("photo", LINE)                         # the alert did not wait
    end = time.time() + 10
    while len(tg.sent) < 2 and time.time() < end:
        time.sleep(0.05)
    assert tg.sent[1] == ("msg", "₿ hammer on S1 · 10:00 AM\n[AI] S1 59,000 held on the close.")
    assert sg.load(r["id"])["ai_json"]["line"] == "S1 59,000 held on the close."


def test_ai_bad_figures_dropped_and_off_never_blocks(monkeypatch):
    tg = telegram(monkeypatch)
    _ai_on(monkeypatch, lambda s, u: "read: a target of 65,432 is likely.")
    c = cfg(**QUIET)
    c.ai.enabled = True
    r = _hammer(c)
    assert tg.sent == [("photo", LINE)] and sg.load(r["id"])["ai_json"]["state"] == "empty"
    assert read.parse_line("read: S1 59,000 holds", "S1 59,000") == "S1 59,000 holds"
    assert read.parse_line("read: 61,000 next", "S1 59,000") is None


def test_candle_model_read_in_shadow(monkeypatch):
    from backend.market import reads as rd
    telegram(monkeypatch)
    got = []

    def worker(payload):
        got.append(payload)
        return {"p_up": 0.61, "reads": [{"horizon": 6, "p_up": 0.61, "touch": {"59055": 0.24, "59155": 0.47}}], "ms": 300}
    monkeypatch.setattr(rd, "WORKER", worker)
    c = cfg(**QUIET)
    c.market.kronos = True
    r = _hammer(c)
    end = time.time() + 5
    while not (sg.load(r["id"])["ai_json"] or {}).get("model") and time.time() < end:
        time.sleep(0.05)
    m = sg.load(r["id"])["ai_json"]["model"]
    assert m["p_up"] == 0.61 and m["stop_first"] == 0.24 and m["r1_first"] == 0.47 and m["show"] == "shadow"
    assert got[0]["levels"] == [59055.0, 59155.0] and got[0]["interval"] == "5m" and got[0]["candles"][-1]["at"] == "2026-11-24 10:00"
    from backend.crypto import forecast
    forecast.drain()
    assert sg.load(r["id"])["fc_json"]["touch_r1"] == 0.47                     # the same read stored as fc_json (the BTC scorecard)


def test_forming_heads_up(monkeypatch):
    tg = telegram(monkeypatch)
    c = cfg(**QUIET)
    cs = hammer_world()
    assert sg.forming(c, T_DAY, now_ts=T_DAY + 250, candles=cs) == []     # off by default
    c.crypto.forming = True
    out = sg.forming(c, T_DAY, now_ts=T_DAY + 250, candles=cs)
    assert out == ["₿ forming · hammer on S1 59,000 · 5 min · 10:00 AM · not confirmed"] and tg.sent[-1] == ("msg", out[0])
    assert sg.forming(c, T_DAY, now_ts=T_DAY + 260, candles=cs) == []      # once per candle
    sg.process(c, T_DAY, now_ts=T_DAY + 303)
    assert tg.sent[-1] == ("msg", "₿ hammer on S1 · 10:00 AM · confirmed at the close")
    # a second candle that looked like a hammer at 80 % and closed red, far from the shape
    t2 = T_DAY + 3600
    feed.upsert("BTCUSD", "5m", [k(T_DAY + i * 300, 59050, 59060, 59040, 59050) for i in range(1, 12)])
    early = cs[:-1] + [k(t2, 59090, 59105, 58990, 59100, 130)]
    early = [dict(b, t=t2 - (len(early) - 1 - i) * 300) for i, b in enumerate(early)]
    sg.forming(c, t2, now_ts=t2 + 250, candles=early)
    assert "forming · hammer" in tg.sent[-1][1]
    feed.upsert("BTCUSD", "5m", [k(t2, 59090, 59105, 58990, 58995, 130)])
    sg.process(c, t2, now_ts=t2 + 303)
    assert tg.sent[-1] == ("msg", "₿ hammer on S1 · 11:00 AM · cancelled · the close did not hold the shape")


def test_snapshot_without_matplotlib_sends_text(monkeypatch):
    from backend.alerts import charts
    tg = telegram(monkeypatch)
    monkeypatch.setattr(charts, "CHARTS_AVAILABLE", False)
    r = _hammer(cfg(**QUIET))
    assert tg.sent == [("msg", LINE)] and r["png_path"] is None


@pytest.mark.parametrize("on", [False, True])
def test_telegram_switch(monkeypatch, on):
    tg = telegram(monkeypatch)
    r = _hammer(cfg(telegram=on, **QUIET))
    assert r["state"] == "sent" and bool(tg.sent) is on
    assert (r["why"] is None) is on


def test_history_scan_marks_past_patterns_without_sending(monkeypatch):
    """The chart showed nothing after the first deploy: detection ran only on candles closed after the feed started.
    scan_history marks what the rules find in the stored candles of the last scan_days, never sends, is idempotent."""
    tg = telegram(monkeypatch)
    c = cfg(**QUIET)
    hammer_world(T_DAY)                                              # stored, never processed live
    now = T_DAY + 2 * 3600
    r = sg.scan_history(c, days=2, now_ts=now)
    assert r["marked"] >= 1 and r["scanned"] >= 30 and r["days"] == 2
    rows_ = sg.rows("pattern = 'hammer'")
    assert len(rows_) == 1 and rows_[0]["state"] == "mark" and rows_[0]["why"] == "history scan"
    assert tg.sent == [] and tg.photos == []                           # nothing on Telegram
    again = sg.scan_history(c, days=2, now_ts=now)
    assert again["marked"] == 0 and len(sg.rows("pattern = 'hammer'")) == 1   # idempotent
    assert sg.scan_history(c, days=0, now_ts=now) == {"scanned": 0, "marked": 0}
    ids = sg.process(c, T_DAY, now_ts=T_DAY + 303)                    # a live pass on the same candle adds no duplicate
    assert ids == [] and len(sg.rows("pattern = 'hammer'")) == 1
