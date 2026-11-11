"""💰 Broker check: the pasted positions (Tradetron's table, a broker CSV, plain lines) parsed tolerantly, the
three-way compare with the open Real positions, the last paste kept and served by GET, and Home's attention item
("paste the positions after the 9:16 send") that appears after a send and clears with a paste. Nothing is sent."""

from backend.engine import attention, broker_check, real
from tests.test_real_edit import opened

TT = ("INSTRUMENT\tQTY\tLTP\tVALUE\tP&L\n"
      "RELIANCE\t8\t1,337.60\t+₹ 10.70 k\t+₹ 346.40\n"
      "DIVISLAB\t2\t11,231.50\t+₹ 22.46 k\t+₹ 2.60 k\n"
      "MOTHERSON\t0\t165.68\t+₹ 0.00\t-₹ 487.63\n"
      "LAURUSLABS\t8\t2,421.00\t+₹ 19.37 k\t-₹ 20.80\n"
      "Total\t\t\t+₹ 2.55 L\t+₹ 7.58 k\n")
ITEM = "Broker check: paste the positions after the 9:16 send"


def test_parse_tradetron_table_with_tabs_total_and_zero_qty():
    rows = broker_check.parse(TT)
    assert [(r["symbol"], r["qty"], r["price"]) for r in rows] == [
        ("RELIANCE", 8, 1337.6), ("DIVISLAB", 2, 11231.5), ("MOTHERSON", 0, 165.68), ("LAURUSLABS", 8, 2421.0)]
    assert [r["empty"] for r in rows] == [False, False, True, False]
    assert all(r["unknown"] is False for r in rows)                      # no universe given: nothing is unknown
    assert rows[0]["source_line"].startswith("RELIANCE")


def test_parse_messy_spacing_rupee_k_suffix_and_noise_lines():
    text = ("INSTRUMENT   QTY   LTP        VALUE       P&L\n"
            "Hide nil qty\n\n"
            "RELIANCE       8   1,337.60   ₹10.70 k    +₹346.40\n"
            "GAIL 113 188.37 ₹21.29 k +₹997.79\n"
            "some words that are not a position\n"
            "Total   ₹2.55 L   +₹7.58 k\n")
    rows = broker_check.parse(text, universe={"RELIANCE"})
    assert [(r["symbol"], r["qty"], r["price"], r["unknown"]) for r in rows] == [("RELIANCE", 8, 1337.6, False), ("GAIL", 113, 188.37, True)]


def test_parse_broker_csv_exports_strip_eq_and_read_the_average():
    z = "Instrument,Qty.,Avg.,LTP,P&L,Chg.\nRELIANCE-EQ,8,\"1,320.10\",1337.6,140,1.2\nM&M,3,2900,2950,150,1\n"
    assert [(r["symbol"], r["qty"], r["price"]) for r in broker_check.parse(z)] == [("RELIANCE", 8, 1320.1), ("M&M", 3, 2900.0)]
    k = "tradingsymbol,exchange,Net Quantity,Buy Average,Sell Average\ntcs-eq,NSE,5,3500.5,0\nINFY-EQ,NSE,0,0,1500\n"
    rows = broker_check.parse(k)
    assert [(r["symbol"], r["qty"], r["price"], r["empty"]) for r in rows] == [("TCS", 5, 3500.5, False), ("INFY", 0, None, True)]
    semi = "Symbol;Quantity;Average Price\nHDFCBANK;10;1650\n"
    assert [(r["symbol"], r["qty"], r["price"]) for r in broker_check.parse(semi)] == [("HDFCBANK", 10, 1650.0)]


def test_parse_plain_lines_and_duplicates_add_up():
    rows = broker_check.parse("reliance 8\nTCS-EQ 5 3500\nRELIANCE 2\n\n")
    assert [(r["symbol"], r["qty"], r["price"]) for r in rows] == [("RELIANCE", 10, None), ("TCS", 5, 3500.0)]
    assert broker_check.parse("") == [] and broker_check.parse("   \n\n") == []


def test_compare_three_ways_with_a_qty_difference_and_an_estimated_entry(monkeypatch):
    c, data, today, sym = opened(monkeypatch)
    pos = {p["symbol"]: p for p in real.summary(c)["positions"]}
    others = [s for s in pos if s != sym]
    text = f"{sym} {pos[sym]['qty'] + 1} {round(pos[sym]['buy'] * 1.01, 2)}\n"           # qty differs, broker price
    if others:
        text += f"{others[0]} {pos[others[0]]['qty']}\n"                                    # qty matches, no price
    text += "ZZZNEW 7 100.5\nEMPTYONE 0 50\n"
    out = broker_check.compare(c, broker_check.parse(text))
    both = {x["symbol"]: x for x in out["both"]}
    x = both[sym]
    assert x["app_qty"] == pos[sym]["qty"] and x["broker_qty"] == pos[sym]["qty"] + 1 and x["match"] is False
    assert x["est"] is True and x["app_entry"] == pos[sym]["buy"] and x["broker_price"] == round(pos[sym]["buy"] * 1.01, 2)
    if others:
        assert both[others[0]]["match"] is True and both[others[0]]["broker_price"] is None
    assert out["both"][0]["symbol"] == sym                                                 # differences first
    assert {a["symbol"] for a in out["app_only"]} == set(others[1:])
    bo = {b["symbol"]: b for b in out["broker_only"]}
    assert bo["ZZZNEW"]["qty"] == 7 and bo["ZZZNEW"]["price"] == 100.5 and bo["ZZZNEW"]["empty"] is False
    assert bo["EMPTYONE"]["empty"] is True
    n = out["counts"]
    assert n["both"] == 1 + bool(others) and n["differ"] == 1 and n["match"] == bool(others)
    assert n["app_only"] == len(others[1:]) and n["broker_only"] == 1 and n["empty"] == 1
    assert n["differences"] == 1 + len(others[1:]) + 1
    assert real.summary(c)["positions"] == list(pos.values())                            # a check only: nothing moved


def test_check_is_kept_and_served_again_with_the_book_as_it_is_now(monkeypatch):
    c, data, today, sym = opened(monkeypatch)
    p = next(x for x in real.summary(c)["positions"] if x["symbol"] == sym)
    out = broker_check.check(c, f"{sym} {p['qty'] + 2}\n")
    assert out["at"] and out["counts"]["differ"] == 1 and out["text"].startswith(sym)
    sv = broker_check.saved()
    assert sv["text"] == f"{sym} {p['qty'] + 2}\n" and sv["at"] == out["at"] and sv["counts"] == out["counts"]
    real.edit_position(c, sym, qty=p["qty"] + 2, note="broker check")                   # the fix made through the edit
    again = broker_check.last(c)
    assert again["at"] == out["at"] and again["text"] == sv["text"]
    assert again["both"][0]["match"] is True and again["counts"]["differ"] == 0            # re-compared with the book now


def test_routes_post_compares_and_saves_get_returns_the_last(monkeypatch):
    from fastapi.testclient import TestClient
    c, data, today, sym = opened(monkeypatch)
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    from backend import main
    with TestClient(main.app) as tc:
        assert tc.get("/api/real/broker-check").json()["at"] is None
        r = tc.post("/api/real/broker-check", json={"text": TT + f"{sym} 1\n"}).json()
        assert r["at"] and r["counts"]["differ"] == 1 and r["counts"]["empty"] == 1
        assert {x["symbol"] for x in r["broker_only"]} >= {"RELIANCE", "DIVISLAB", "MOTHERSON", "LAURUSLABS"}
        g = tc.get("/api/real/broker-check").json()
        assert g["at"] == r["at"] and g["text"] == TT + f"{sym} 1\n" and g["counts"] == r["counts"]
        assert tc.post("/api/real/broker-check", json={}).status_code == 422


def test_attention_asks_for_a_paste_after_a_send_and_clears_with_one(monkeypatch):
    c, data, today, sym = opened(monkeypatch)                                              # today's BUYs were sent
    assert broker_check.stale_since() == today
    items = attention._real(c, False)
    assert ("filter", ITEM) in items and not any(lvl == "warn" and "Broker" in t for lvl, t in items)
    broker_check.check(c, f"{sym} 1\n")                                                    # a paste now (after that day)
    assert broker_check.stale_since() is None
    assert ITEM not in [t for _, t in attention._real(c, False)]
    sig = attention.signals(c, False)["real"]
    assert ITEM not in [i["text"] for i in sig["items"]]
