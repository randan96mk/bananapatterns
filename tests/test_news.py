"""The News tab: board scope (every screen and stage, plus held / watched), filings / red flags / fund moves in one
list without duplicates, the results list, and the API."""

from backend.alerts import guard
from backend.config import AlertsConfig, VcpConfig
from backend.database.models import get_db, now_ist
from backend.ingest import filings as fl
from backend.ingest import holders as hd
from backend.ingest import news as nw
from tests.test_cups import cup_series, scan_with
from tests.test_events import results_in
from tests.test_filings import ann, stamp
from tests.test_holders import BULK, SAST


def board():
    scan_with({"AAA": cup_series(breakout=False, handle_depth=0.04),          # cup screen + bases
               "DDD": cup_series(breakout=False, handle_depth=0.04, handle_vol=1.6)})   # loud handle: bases only
    guard.sync({"HELD": {"qty": 1, "avg": 10.0}}, VcpConfig())
    with get_db() as conn:
        conn.execute("INSERT INTO watchlist (symbol, added_at) VALUES ('WATCH', ?)", (now_ist(),))


def test_scope_covers_every_screen_plus_held_and_watched():
    board()
    scope = nw.board_scope()
    assert scope["HELD"] == "held" and scope["WATCH"] == "watching"
    assert scope["AAA"] == "C&H forming"                                  # the most specific screen wins
    assert scope["DDD"].startswith("Base ")
    assert "OUT" not in scope


def test_board_news_groups_and_no_duplicates():
    board()
    hd.store(hd.parse_deals_csv(BULK, "bulk") + hd.parse_sast(SAST))      # AAA: fund bought; DDD: promoter sold
    fl.store(fl.parse_nse([
        ann(1, "AAA", "Outcome of Board Meeting", "the financial results for the quarter", stamp(5)),
        ann(8, "AAA", "Press Release", "Press Release for Financial Results", stamp(4.9)),  # same day: one line
        ann(2, "HELD", "Bagging/Receiving of orders/contracts", "order of Rs 200 Cr", stamp(3)),
        ann(3, "WATCH", "Action(s) taken or orders passed", "SEBI order", stamp(2)),        # a red flag
        ann(4, "WATCH", "Action(s) taken or orders passed", "order passed in favour of the Company", stamp(2)),
        ann(5, "OUT", "Acquisition", "not on the board", stamp(1)),
        ann(6, "AAA", "Trading Window", "noise", stamp(1)),
        ann(7, "AAA", "Press Release", "an update", stamp(1))]))
    results_in("AAA", 2)
    results_in("HELD", 2)
    results_in("DDD", 9)                                                   # outside the warning window
    n = nw.board_news(AlertsConfig())
    got = [(i["group"], i["symbol"], i["label"]) for i in n["items"]]
    assert ("results", "AAA", "Results") in got and ("deals", "HELD", "Order win") in got
    assert ("red", "WATCH", "Regulatory / legal") in got and ("other", "WATCH", "Regulatory / legal") in got
    assert ("red", "DDD", "Promoter sold 2.4%") in got and ("funds", "AAA", "Fund bought ₹60 Cr") in got
    assert sum(1 for g in got if g[1] == "WATCH") == 2                     # the SEBI order once, as a red flag
    assert not any(g[1] == "OUT" for g in got) and not any(g[2] in ("Filing", "Update") for g in got)
    assert n["counts"] == {"results": 1, "deals": 1, "red": 2, "other": 1, "funds": 1}
    assert [r["symbol"] for r in n["results"]] == ["HELD", "AAA"]          # same day: held first
    assert [nw._why_rank(w) for w in ("held", "watching", "VCP forming", "Base played out")] == [0, 1, 2, 3]
    assert n["items"] == sorted(n["items"], key=lambda i: i["dt"], reverse=True)


def test_news_api(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    board()
    fl.store(fl.parse_nse([ann(1, "AAA", "Bagging/Receiving of orders/contracts", "big order", stamp(2))]))
    from backend import main
    with TestClient(main.app) as tc:
        d = tc.get("/api/news").json()
        assert d["items"][0]["symbol"] == "AAA" and d["items"][0]["why"] == "C&H forming" and d["scope"] == 4
        assert tc.get("/api/news?days=0").status_code == 422
