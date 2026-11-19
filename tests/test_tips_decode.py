"""💡 Decode: the 15 real calls of the fixture (every shape the channel used), the aliases, expiry lines, target
shapes, spot stops, Target 0, sizes, and the model's fill (kept only when the figure is in the text)."""

import pytest
from datetime import date

from backend.tips import decode as dc
from tests.tips_world import posts

HINTS = [p for p in posts() if p["kind"] == "hint"]


@pytest.mark.parametrize("p", HINTS, ids=[str(p["post_id"]) for p in HINTS])
def test_fixture_calls_decode(p):
    d = dc.decode(p["text"], p["posted_at"])
    e = p["expect"]
    assert dc.looks_like_hint(d, p["text"])
    assert d["symbol"] == e["symbol"] and d["side"] == e["side"]
    assert d["strike"] == e["strike"] and d["opt_type"] == e["opt_type"]
    mode, lo, hi, wc = e["entry"]
    assert (d["entry"]["mode"], d["entry"]["lo"], d["entry"]["hi"]) == (mode, lo, hi)
    assert d["entry"]["when_comes"] == wc or mode in ("above", "below")
    assert (d["stop"]["kind"], d["stop"]["value"]) == tuple(e["stop"])
    assert d["targets"] == e["targets"]
    if "open_ended" in e:
        assert d["open_ended"] == e["open_ended"]
    if "expiry" in e:
        assert d["expiry"] == e["expiry"] and d["expiry_src"] == "post"
    if "instrument" in e:
        assert d["instrument"] == e["instrument"]
    assert all(v in ("code", "strike") for v in d["decoded"].values())        # "strike": the index read from the level


@pytest.mark.parametrize("text,sym", [("Buy BNF 51500 PE above 355", "BANKNIFTY"), ("buy bank nifty 50300 PE at 10", "BANKNIFTY"),
                                      ("BANKNIFTY 49600 PE", "BANKNIFTY"), ("buy FIn nifty 23200 CE", "FINNIFTY"),
                                      ("Sell 21550 ce in FN at 8", "FINNIFTY"), ("SENSEX 75200 Ce", "SENSEX"),
                                      ("BUY MIDCP 12650 pe", "MIDCPNIFTY"), ("MIDCPNIFTY 12650 pe", "MIDCPNIFTY"),
                                      ("Buy CRUDE 5300 PE", "CRUDE"), ("nifty 24150 PE", "NIFTY")])
def test_aliases(text, sym):
    assert dc.symbol_of(text) == sym


def test_spot_stop_15_min_candle_and_target_zero():
    t = "For 8th November, 2023 expiry \n\nSell Bank nifty 43600 CE at 36\ntarget 0 \nStop loss - 15 mins candle above 43450 spot"
    d = dc.decode(t, "2023-11-07")
    assert d["stop"] == {"kind": "spot", "value": 43450, "dir": "above", "tf": 15}
    assert d["targets"] == [0] and d["side"] == "SELL" and d["expiry"] == "2023-11-08"


def test_target_shapes_and_zone_shorthand():
    assert dc.decode("buy nifty 24400 ce at 90\nSL 70\nTgt 120/130/140/150++", "2026-11-17")["targets"] == [120, 130, 140, 150]
    d = dc.decode("BANKNIFTY 49600 PE BUY ABOVE 50\nSL 40\nTG 60 80 100 PLUS", "2026-11-17")
    assert d["targets"] == [60, 80, 100] and d["open_ended"]
    z = dc.decode("Buy nifty 24400 PE near 50200-300 when comes\nSL 100\nTgt 400", "2026-11-17")["entry"]
    assert (z["lo"], z["hi"]) == (50200, 50300)


def test_lots_qty_and_cmp():
    assert dc.decode("Buy nifty 24400 CE at 100 SL 80 Tgt 130 · 2 lots", "2026-11-17")["size_hint"] == {"lots": 2}
    assert dc.decode("Buy nifty 24400 CE at 100 SL 80 Tgt 130 qty 150", "2026-11-17")["size_hint"] == {"qty": 150}
    assert dc.decode("Buy nifty 24400 CE cmp 93 SL 80 Tgt 130", "2026-11-17")["entry"]["mode"] == "at"


def test_expiry_line_year_rolls_over():
    assert dc.decode("Sell 78700 PE Sensex 2nd Jan at 83\ntarget 0\nSL 100", "2026-12-30")["expiry"] == "2027-01-02"


def test_follow_ups_and_chatter_are_not_hints():
    for t in ("2 targets done ✅", "Nifty 22200 CE\n320❤️\n\nTgt 4 Done ✅", "Midcp 12650 pe in watch", "25200 ce writers better be cautious",
              "82200 CE bought near 440 ➡️ Target hit near 500 !"):
        assert not dc.looks_like_hint(dc.decode(t, "2026-11-17"), t), t


def test_model_fill_only_from_the_text():
    t = "Buy 22950 ce at 90-93\nTgt 120/130"
    d = dc.decode(t, "2026-11-17", fill=lambda text, missing: {"stop": {"kind": "premium", "value": 70}, "symbol": "NIFTY"})
    assert d["stop"]["kind"] == "none" and "stop" not in d["decoded"]          # 70 is not in the text: dropped
    assert d["symbol"] == "NIFTY" and d["decoded"]["symbol"] == "strike"      # not from the model: read from the strike level
    t2 = "Buy nifty 22950 ce at 90-93\nstoploss below premium 70 please\nTgt 120"
    d2 = dc.decode(t2.replace("stoploss", "risk"), "2026-11-17", fill=lambda text, missing: {"stop": {"kind": "premium", "value": 70}})
    assert d2["stop"] == {"kind": "premium", "value": 70} and d2["decoded"]["stop"] == "ai"


def test_title():
    d = dc.decode("Buy bank nifty 50300 PE near 250-270 when comes\nSl 190\nTgt 320", "2026-11-17")
    assert dc.title(d) == "BUY BANKNIFTY 50300 PE"


def test_an_index_option_with_no_name_is_read_from_the_strike_level():
    from datetime import datetime
    from backend.config import IST
    from backend.tips import decode as dc
    now = datetime.now(IST)
    d = dc.decode("buy 82400 pe near 680-700 cmp\n2 lots", now)
    assert d["symbol"] == "SENSEX" and d["decoded"]["symbol"] == "strike" and d["strike"] == 82400 and d["opt_type"] == "PE"
    assert dc.decode("buy 25900 ce at 100 sl 80", now)["symbol"] == "NIFTY"
    assert dc.decode("sell 58200 pe at 140", now)["symbol"] == "BANKNIFTY"
    assert dc.decode("buy 40000 ce at 10", now).get("symbol") is None            # between the bands: never guessed
    d = dc.decode("BUY SENSEX 82000 CE NEAR 460", now)
    assert d["symbol"] == "SENSEX" and d["decoded"]["symbol"] == "code"         # a named index stays "code"


# ⚡ PR C: the entry level is the option's premium or the index; wait words gate only an entry with no level

@pytest.mark.parametrize("text,kind,mode,lo", [
    ("BUY SENSEX 82300 CE ONLY ABOVE 370\nSL 200\nTGT 500\nWAIT FOR ACTIVATION", "premium", "above", 370),
    ("buy 82400 pe near 680-700 cmp\nsl 600\ntgt 800", "premium", "zone", 680),
    ("BANKNIFTY 49600 PE BUY ABOVE 50 \n\nSL 40 \n\nTG 60 80 100 PLUS", "premium", "above", 50),
    ("Sell Bank nifty 43600 CE at 36\ntarget 0\nStop loss - 15 mins candle above 43450 spot", "premium", "at", 36),
    ("nifty above 25300 buy 25300 ce\nsl 80\ntgt 150", "spot", "above", 25300),
    ("BUY SENSEX 82300 CE above 82400 spot\nSL 200\nTGT 500", "spot", "above", 82400),
    ("buy sensex 82300 ce sensex above 82400 sl 200 tgt 500", "spot", "above", 82400),
    ("BUY NIFTY 25300 CE ABOVE 25320 SL 80 TGT 150", "spot", "above", 25320),
])
def test_level_kind_premium_or_index(text, kind, mode, lo):
    from backend.tips import plan as pl
    d = dc.decode(text, date(2026, 11, 19))
    e = d["entry"]
    assert (e["level_kind"], e["mode"], e["lo"]) == (kind, mode, lo) and d["decoded"]["entry_kind"] == "code"
    t = pl.entry_text(e)
    assert t == (f"{d['symbol']} {mode} {pl.fmt(lo)}" if kind == "spot" else t)
    if kind == "spot":
        assert e["index"] == d["symbol"] and "," in t


def test_activation_words_only_gate_an_entry_without_a_level():
    d = dc.decode("BUY SENSEX 82300 CE ONLY ABOVE 370\nSL 200\nTGT 500\nWAIT FOR ACTIVATION", date(2026, 11, 19))
    assert d["entry"]["wait_words"] and not d["entry"]["needs_activation"]           # the level is the trigger
    for w in ("wait for activation", "wait for conditional buy", "WAIT FOR OUR CONFIRMATION", "dont enter now"):
        d = dc.decode(f"BUY NIFTY 25900 PE NEAR 95-100 {w}\nSL 80\nTGT 120", date(2026, 11, 19))
        assert d["entry"]["needs_activation"], w
    d = dc.decode("BUY NIFTY 25900 PE\nSL 80\nTGT 120\nwait for activation", date(2026, 11, 19))
    assert d["entry"]["mode"] == "market" and d["entry"]["needs_activation"]
    assert not dc.decode("BUY NIFTY 25900 PE NEAR 95-100\nSL 80\nTGT 120", date(2026, 11, 19))["entry"]["needs_activation"]


@pytest.mark.parametrize("text,action,value", [
    ("REST STOPLOSS 530", "stop", 530), ("rest sl 370", "stop", 370), ("REVISE SL 298", "stop", 298), ("sl now 298", "stop", 298),
    ("tsl gone for rest quantity", "sl_hit", None), ("sl gone", "sl_hit", None), ("tsl hit", "sl_hit", None), ("stoploss gone", "sl_hit", None),
    ("BOOK PART & KEEP TSL AT COST FOR REST QTY", "partial", None), ("book part qty and keep TSL at cost", "partial", None),
    ("ADD MORE IF COMES NEAR 440", "add", None), ("add more 1 lot near 440", "add", None), ("add 5 lots at 560 cmp", "add", None),
    ("570", "price", None), ("580 again", "price", None), ("585", "price", None), ("614", "price", None), ("455", "price", None),
    ("TSL at cost", "trail", None), ("activated", "active", None),
])
def test_19_nov_follow_up_shapes(text, action, value):
    from backend.tips import plan as pl
    a = pl.followup_action(text)
    assert a["action"] == action and (value is None or a["value"] == value)
    if action == "partial":
        assert a["cost"] is True
    if action == "add":
        assert not dc.looks_like_hint(dc.decode(text, date(2026, 11, 19)), text)
