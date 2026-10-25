"""Red flags and fund moves: bulk / block deals and SAST Reg 29 disclosures (parse, store, fetch), the signals
built from them and the filings, tier B on a red flag, alert lines, held / watched / plan alerts, plan, API."""

from datetime import date, datetime, timedelta

import httpx
import pytest

from backend.alerts import guard
from backend.alerts import plan as pl
from backend.alerts.dispatch import Notifier, flags_note
from backend.config import IST, AlertsConfig, VcpConfig
from backend.database.models import now_ist
from backend.engine import market
from backend.ingest import filings as fl
from backend.ingest import holders as hd
from backend.ingest.holders import refresh_from_nse as real_refresh     # bound before conftest blocks the network
from tests.test_alerts import FakeTG
from tests.test_filings import ann, stamp, scheduler

TODAY = now_ist()[:10]


def ago(days: int) -> str:
    return (date.fromisoformat(TODAY) - timedelta(days=days)).isoformat()


def nse_day(days: int) -> str:
    return datetime.strptime(ago(days), "%Y-%m-%d").strftime("%d-%b-%Y").upper()


BULK = ("Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n"
        f"{nse_day(1)},AAA,Aaa Ltd,HDFC MUTUAL FUND,BUY,1000000,600.00,-\n"           # ₹60 Cr: a fund buying
        f"{nse_day(1)},AAA,Aaa Ltd,IRAGE BROKING SERVICES LLP,BUY,500000,600.00,-\n"   # a prop trader: not a fund
        f"{nse_day(1)},BBB,Bbb Ltd,ALPHA GROWTH FUND,BUY,100000,500.00,-\n"           # round trip: nets out
        f"{nse_day(1)},BBB,Bbb Ltd,ALPHA GROWTH FUND,SELL,100000,501.00,-\n"
        f"{nse_day(1)},CCC,Ccc Ltd,SMALL CAP FUND,BUY,10000,100.00,-\n")              # ₹0.1 Cr: too small
BLOCK = ("Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\n"
         "NO RECORDS,,,,,,\n")


def sast(sym, name, kind, pct, after, promoter, days=1):
    stamp_ = datetime.strptime(ago(days), "%Y-%m-%d").strftime("%d-%b-%Y") + " 17:05"
    return {"symbol": sym, "acquirerName": name, "acqSaleType": kind, "promoterType": "Y" if promoter else "N",
            "totAcqShare": str(pct) if kind == "Acquisition" else None,
            "totSaleShare": str(pct) if kind == "Sale" else None, "totAftShare": str(after),
            "noOfShareAcq": "100", "noOfShareSale": "100", "timestamp": stamp_,
            "attachement": f"https://nsearchives.nseindia.com/corporate/{sym}.zip"}


SAST = {"data": [sast("DDD", "Promoter Family Trust", "Sale", 2.4, 48.1, True),
                 sast("EEE", "Promoter Holdings", "Sale", 0.4, 60.0, True),                  # under 1%: not red
                 sast("FFF", "ICICI Prudential Mutual Fund", "Acquisition", 2.1, 5.2, False),
                 sast("GGG", "Gaurav Goyal", "Acquisition", 3.0, 6.0, False),                # a person, not a fund
                 sast("III", "Mirae Asset Mutual Fund", "Sale", 0.04, 2.95, False),          # a threshold crossing
                 sast("HHH", "Old Promoter", "Sale", 5.0, 30.0, True, days=45),             # too old
                 {"symbol": "ZZZ", "acqSaleType": "Both", "timestamp": "23-Oct-2026 10:00"}]}


@pytest.mark.parametrize("name,inst", [("HDFC MUTUAL FUND", True), ("Funds under the management of FMR LLC", True),
                                       ("LIFE INSURANCE CORPORATION OF INDIA", True), ("SBI LIFE INSURANCE", True),
                                       ("IRAGE BROKING SERVICES LLP", False), ("Gaurav Goyal", False),
                                       ("GRAVITON RESEARCH CAPITAL LLP", False)])
def test_institutional(name, inst):
    assert hd.institutional(name) is inst


def test_parse():
    rows = hd.parse_deals_csv(BULK, "bulk")
    assert len(rows) == 5 and rows[0] == {"date": ago(1), "symbol": "AAA", "client": "HDFC MUTUAL FUND", "side": "BUY",
                                          "qty": 1000000.0, "price": 600.0, "source": "bulk", "pct": None,
                                          "after_pct": None, "promoter": 0, "url": ""}
    assert hd.parse_deals_csv(BLOCK, "block") == []
    s = hd.parse_sast(SAST)
    assert len(s) == 6 and s[0]["side"] == "SELL" and s[0]["pct"] == 2.4 and s[0]["after_pct"] == 48.1
    assert s[0]["promoter"] == 1


def test_fetch_store_and_partial_failure():
    def handler(req):
        if req.url.path.endswith("bulk.csv"):
            return httpx.Response(200, text=BULK)
        if req.url.path.endswith("block.csv"):
            return httpx.Response(200, text=BLOCK)
        if req.url.path == "/api/corporate-sast-reg29":
            return httpx.Response(200, json=SAST)
        return httpx.Response(403)

    assert len(real_refresh(client=httpx.Client(transport=httpx.MockTransport(handler)))) == 11
    assert real_refresh(client=httpx.Client(transport=httpx.MockTransport(handler))) == []        # deduplicated

    def sast_down(req):
        return httpx.Response(500) if "sast" in req.url.path else handler(req)
    assert real_refresh(client=httpx.Client(transport=httpx.MockTransport(sast_down))) == []      # partial: kept going
    with pytest.raises(RuntimeError):
        real_refresh(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))


def seed():
    hd.store(hd.parse_deals_csv(BULK, "bulk") + hd.parse_sast(SAST))
    fl.store(fl.parse_nse([
        ann(1, "LEG", "Action(s) taken or orders passed", "SEBI passed an order imposing a penalty", stamp(30)),
        ann(2, "WIN", "Action(s) taken or orders passed", "order passed in favour of the Company", stamp(30)),
        ann(3, "AUD", "Resignation of Statutory Auditor", "statutory auditor resigned", stamp(30)),
        ann(4, "RAT", "Credit Rating- Revision", "CRISIL downgraded the rating to A-", stamp(30)),
        ann(5, "RAT2", "Credit Rating- Revision", "rating upgraded", stamp(30))]))


def test_signals():
    seed()
    cfg = AlertsConfig()
    s = hd.signals(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "LEG", "WIN", "AUD", "RAT", "RAT2"], cfg)
    assert set(s) == {"AAA", "DDD", "FFF", "LEG", "AUD", "RAT"}
    assert s["AAA"]["buy"][0]["label"] == "Fund bought ₹60 Cr" and "HDFC MUTUAL FUND" in s["AAA"]["buy"][0]["text"]
    assert s["DDD"]["red"][0]["label"] == "Promoter sold 2.4%" and "now 48.1%" in s["DDD"]["red"][0]["text"]
    assert s["FFF"]["buy"][0]["label"] == "Fund stake up to 5.2%"
    assert [s[k]["red"][0]["label"] for k in ("LEG", "AUD", "RAT")] == ["Regulatory / legal", "Auditor resigned",
                                                                        "Rating downgrade"]
    assert hd.signals(["DDD"], AlertsConfig(red_flags=False)) == {}
    assert hd.signals(["AAA"], AlertsConfig(deal_min_cr=100)) == {}


def test_red_flag_holds_tier_b():
    seed()
    cfg = AlertsConfig()
    p = {"vcp": {"stop": 1}, "rs": 95, "tt_passed": 8, "industry_rank": 1, "industry_of": 9}
    assert market.tier(dict(p, symbol="AAA"), market.UPTREND, cfg) == "A"
    assert market.tier(dict(p, symbol="DDD"), market.UPTREND, cfg) == "B"
    assert market.tier(dict(p, symbol="DDD"), market.UPTREND, AlertsConfig(red_flags=False)) == "A"


def test_alert_lines():
    seed()
    note = flags_note("DDD", AlertsConfig())
    assert note.startswith("\n🚩 <b>Promoter sold 2.4%</b>") and "Held at tier B" in note
    assert flags_note("AAA", AlertsConfig()).startswith("\n🏦 HDFC MUTUAL FUND bought ₹60.0 Cr")
    assert flags_note("CCC", AlertsConfig()) == ""
    fake = FakeTG()
    Notifier(AlertsConfig(chat_id="42"), fake).send("k", "near_pivot", "DDD near pivot", "DDD")
    assert "🚩" in fake.sent[0][1]


def test_held_and_plan_stocks_get_deal_alerts():
    guard.sync({"DDD": {"qty": 5, "avg": 100.0}}, VcpConfig())
    pl.save_plan(TODAY, [{"symbol": "AAA", "pivot": 10.0}])
    new = hd.store(hd.parse_deals_csv(BULK, "bulk") + hd.parse_sast(SAST))
    sch, fake = scheduler()
    assert sch.alert_deals(new) == 2
    texts = [h for _, h in fake.sent]
    assert any("🚩 <b>DDD</b> · Promoter sold 2.4% <i>(you hold it)</i>" in t and "tier B" in t for t in texts)
    assert any("🏦 <b>AAA</b> · Fund bought ₹60 Cr <i>(in the plan)</i>" in t for t in texts)
    assert sch.alert_deals(new) == 0                                     # once each


def test_plan_bonus_and_lines(monkeypatch):
    from backend.alerts import messages as msg
    seed()
    monkeypatch.setattr(pl, "candidates", lambda as_of, screens: [
        {"symbol": "AAA", "screen": "vcp", "pivot": 100.0, "dist_to_pivot_pct": 2.0, "close": 98.0, "score": 60},
        {"symbol": "XXX", "screen": "vcp", "pivot": 100.0, "dist_to_pivot_pct": 2.0, "close": 98.0, "score": 60},
        {"symbol": "DDD", "screen": "vcp", "pivot": 100.0, "dist_to_pivot_pct": 2.0, "close": 98.0, "score": 60}])
    items = {i["symbol"]: i for i in pl.build_plan(TODAY, AlertsConfig(tiering=False), VcpConfig(), {})}
    assert items["AAA"]["rank"] == items["XXX"]["rank"] + pl.FUND_BONUS
    text = msg.top_setups(list(items.values()), TODAY)
    assert "🏦 Fund bought ₹60 Cr" in text and "🚩 Promoter sold 2.4%" in text


def test_api(monkeypatch):
    from fastapi.testclient import TestClient
    from tests.test_cups import cup_series, scan_with
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    scan_with({"DDD": cup_series(breakout=False, handle_depth=0.04)})
    seed()
    from backend import main
    with TestClient(main.app) as tc:
        row = tc.get("/api/screens/cup/results?stage=forming").json()["rows"][0]
        assert row["flags"]["red"][0]["label"] == "Promoter sold 2.4%" and row["tier"] == "B"
        d = tc.get("/api/deals?symbol=ddd").json()
        assert d["deals"][0]["source"] == "sast" and d["signals"]["red"]
        assert tc.get("/api/deals").json()["last_fetch"]
        assert tc.post("/api/deals/refresh").status_code == 502
