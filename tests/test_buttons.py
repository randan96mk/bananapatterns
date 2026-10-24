"""Alert buttons (Watch / Mute / Chart / I bought), watch / mute effects, manual positions, new bot commands."""

import json
from datetime import datetime

from backend.alerts import guard, plan as pl
from backend.alerts.commands import BotCommands
from backend.alerts.dispatch import Notifier
from backend.alerts.priority import MarketContext, gate
from backend.alerts.telegram import keyboard
from backend.config import IST, AlertsConfig, VcpConfig
from backend.database.models import get_db
from tests.test_login import BotTG, LoginState, update

NOW = datetime(2026, 10, 26, 10, 0, 30, tzinfo=IST)


class TG(BotTG):
    def __init__(self, updates=()):
        super().__init__(list(updates))
        self.answers = []

    def answer_callback(self, cid, text=""):
        self.answers.append(text)


def tap(uid, data, chat=42):
    return {"update_id": uid, "callback_query": {"id": f"cb{uid}", "data": data,
                                                 "message": {"chat": {"id": chat}}}}


def bot(updates):
    st, fake = LoginState(), TG(updates)
    return BotCommands(st, clock=lambda: NOW), Notifier(st.config.alerts, fake), fake


def test_keyboard_and_auto_buttons():
    kb = json.loads(keyboard([("Open", "https://x"), ("📌 Watch", None, "w:ABC")]))
    assert kb == {"inline_keyboard": [[{"text": "Open", "url": "https://x"},
                                       {"text": "📌 Watch", "callback_data": "w:ABC"}]]}
    fake = TG()
    n = Notifier(AlertsConfig(chat_id="42"), fake)
    n.send("k1", "breakout", "x", "ABC")
    assert [b[2] for b in fake.buttons] == ["w:ABC", "m:ABC", "c:ABC", "b:ABC"]
    n.send("k2", "system", "y")
    assert fake.buttons is None
    off = Notifier(AlertsConfig(chat_id="42", alert_buttons=False), fake)
    off.send("k3", "breakout", "z", "ABC")
    assert fake.buttons is None


def test_watch_and_mute_buttons_change_the_gate():
    b, n, fake = bot([tap(1, "w:AAA"), tap(2, "m:BBB"), tap(3, "w:EVIL", chat=999)])
    assert b.poll_once(n) == 2 and fake.answers[0].startswith("📌 Watching AAA")
    assert "Muted BBB until 2026-11-25" in fake.answers[1]
    cfg = AlertsConfig()
    mc = MarketContext.load(cfg)
    assert mc.watched == {"AAA"} and mc.muted == {"BBB"}
    weak = {"symbol": "AAA", "rs": 40}
    assert gate("breakout", weak, mc, cfg) == (True, "B")                    # watched: sent even as B
    strong = {"symbol": "BBB", "vcp": {"stop": 1}, "rs": 95, "tt_passed": 8, "industry_rank": 1, "industry_of": 9}
    assert gate("breakout", strong, mc, cfg) == (False, None)                # muted: held
    assert gate("below_pivot", strong, mc, cfg) == (True, None)              # sell-side still comes
    b2, n2, fake2 = bot([tap(4, "w:BBB")])
    b2.poll_once(n2)
    mc = MarketContext.load(cfg)
    assert "BBB" in mc.watched and "BBB" not in mc.muted                     # watching un-mutes


def test_chart_and_bought_buttons(monkeypatch):
    monkeypatch.setattr("backend.alerts.preview.alert_chart", lambda cfg, sym, kind="auto": (b"\x89PNG", "near_pivot"))
    monkeypatch.setattr("backend.alerts.preview.resolve_stock", lambda conn, sym: ({"instrument_token": 5}, {}))
    monkeypatch.setattr("backend.alerts.commands.get_candles", lambda conn, tok, limit=None: [{"close": 250.0}])
    b, n, fake = bot([tap(1, "c:CUB"), tap(2, "b:CUB")])
    assert b.poll_once(n) == 2
    assert fake.sent[0][0] == "photo" and "CUB" in fake.sent[0][1]
    assert "Following <b>CUB</b> from ₹250.00, stop ₹230.00" in fake.sent[1][1]
    p = guard.positions()[0]
    assert p["source"] == "manual" and p["qty"] == 0 and p["stop"] == 230.0


def test_manual_position_survives_sync_and_becomes_kite_backed():
    guard.add_manual("MAN", 100.0, VcpConfig())
    guard.sync({"K": {"qty": 5, "avg": 10.0}}, VcpConfig())
    assert {p["symbol"] for p in guard.positions()} == {"MAN", "K"}
    guard.sync({}, VcpConfig())
    assert {p["symbol"] for p in guard.positions()} == {"MAN"}               # sold K drops; manual stays
    guard.sync({"MAN": {"qty": 7, "avg": 101.0}}, VcpConfig())
    p = guard.positions()[0]
    assert p["source"] == "kite" and p["qty"] == 7


def test_commands():
    pl.save_plan("2026-10-23", [{"symbol": "TOP1", "base_label": "B1", "pivot": 100.0, "dist_to_pivot_pct": 1.0,
                                 "zone_top": 105.0, "stop": 92.0, "tier": "A", "coiled": None}])
    b, n, fake = bot([update(1, "/top"), update(2, "/watch abc"), update(3, "/watch"), update(4, "/mute xyz"),
                      update(5, "/unwatch ABC"), update(6, "/watch")])
    assert b.poll_once(n) == 6
    texts = [h for _, h in fake.sent]
    assert "TOP1" in texts[0] and "Watching ABC" in texts[1] and texts[2] == "📌 Watching: ABC"
    assert "Muted XYZ" in texts[3] and "Stopped watching ABC" in texts[4] and texts[5] == "📌 Watching: nothing yet"
    with get_db() as conn:
        assert [r[0] for r in conn.execute("SELECT symbol FROM muted")] == ["XYZ"]
