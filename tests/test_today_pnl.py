"""₹ Today (the status bar chip, stream topic `today_pnl`, GET /api/today-pnl): Kite (live) + real in the total, paper
and a dry run shown but never counted, mode off leaves the Kite part out, hidden before 9 AM and when nothing happened."""

import json
from pathlib import Path

from backend.database.models import get_db
from backend.engine.today_pnl import today_pnl
from backend.tips import kite_orders as ko
from tests.test_tips_home import _day
from tests.test_tips_kite import Broker
from tests.test_tips_kite_auto import pushed, t19  # noqa: F401
from tests.tips_world import BN12, KEY12, at, cfg, seed, world

ROOT = Path(__file__).resolve().parent.parent
KEYS = {"visible", "total", "total_text", "tone", "parts", "open", "title", "at"}


def _paper_and_real(day: str):
    with get_db() as conn:
        conn.execute("INSERT INTO tips_trades (hint_id, opened_at, closed_at, side, symbol, lots, qty, entry_px, exit_px, gross, charges, net, why) "
                     "VALUES (9, ?, ?, 'BUY', 'X', 1, 15, 100, 113, 195, 9, 186, 't1')", (f"{day}T10:00:00+05:30", f"{day}T11:00:00+05:30"))
        conn.execute("INSERT INTO real_state (id, payload) VALUES (1, ?)",
                     (json.dumps({"cash": 50000.0, "positions": {"INFY": {"qty": 10, "last": 1500.0}}, "capital": 65000.0, "deposits": []}),))
        conn.execute("INSERT INTO real_equity (day, equity, gross) VALUES ('2026-11-16', 64700.0, 0)")


def test_shape_live_kite_paper_and_real(pushed):  # noqa: F811
    c = cfg()
    c.tips.kite_mode = "click"
    b = Broker(world(**{KEY12: 262.0}))
    hid = seed(c, b, BN12)["hint_id"]
    ko.place_all(c, b, hid, 2, now=at("10:00"))
    b.fill(b.last(transaction_type="BUY")["order_id"], 262.0)
    ko.sync(c, b, at("10:00", sec=10))
    with get_db() as conn:
        conn.execute("INSERT INTO tips_quotes (hint_id, t, ltp, spot) VALUES (?, ?, 280.0, NULL)", (hid, at("10:05").isoformat()))
    _paper_and_real("2026-11-17")
    d = today_pnl(c, at("10:06"))
    assert set(d) == KEYS and d["visible"] and d["open"]
    p = {x["key"]: x for x in d["parts"]}
    assert p["kite"]["pnl"] == (280 - 262) * 30 and p["kite"]["counted"] and p["kite"]["text"] == "Kite +₹540"
    assert p["paper"]["pnl"] == 186.0 and not p["paper"]["counted"] and p["paper"]["text"] == "paper +₹186"
    assert p["real"]["pnl"] == 65000.0 - 64700.0 and p["real"]["counted"] and p["real"]["text"] == "real +₹300"
    assert d["total"] == 540 + 300 and d["total_text"] == "+₹840" and d["tone"] == "up"         # paper never summed
    assert "paper is not counted in the total" in d["title"]
    c.tips.kite_mode = "off"                                          # off: the Kite part is left out
    d = today_pnl(c, at("10:06"))
    assert [x["key"] for x in d["parts"]] == ["paper", "real"] and d["total"] == 300.0


def test_dry_is_shown_never_counted(pushed):  # noqa: F811
    c, w, s, h1, h2, h3 = _day()
    c.tips.kite_mode = "dry"
    d = today_pnl(c, t19("14:12"))
    k = d["parts"][0]
    assert k["key"] == "kite" and k["label"] == "dry" and not k["counted"] and k["pnl"] == 1400.0 + 480.0
    assert k["text"] == "dry +₹1,880" and d["total"] == 0.0 and d["visible"]
    assert "a dry run is not counted either" in d["title"]


def test_hidden_before_nine_and_when_nothing_happened():
    c = cfg()
    c.tips.kite_mode = "auto"
    d = today_pnl(c, at("10:00"))
    assert not d["visible"] and d["total"] == 0.0                      # nothing traded, every part zero
    _paper_and_real("2026-11-17")
    assert today_pnl(c, at("10:00"))["visible"]
    assert not today_pnl(c, at("08:59"))["visible"]                   # before 9:00 AM


def test_route_topic_and_the_chip():
    from backend.api import routes
    assert "today_pnl" in routes._stream_topics()
    src = (ROOT / "frontend/src/components/StatusBar.jsx").read_text()
    assert "useLive('today_pnl')" in src and "onGo('home')" in src and "hide-phone" in src
