import pytest

from backend.config import IngestConfig
from backend.ingest import universe as uni

CSV = ("﻿Company Name,Industry,Symbol,Series,ISIN Code\n"
       "ABB India Ltd.,Capital Goods,ABB,EQ,INE117A01022\n"
       "Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021\n"
       "Delisted Co,Chemicals,GONE,EQ,INE000000000\n"
       "HFCL Ltd.,Telecommunication,HFCL,BE,INE548A01028\n"
       "Embassy Office Parks REIT,Realty,EMBASSY,RR,INE041025011\n")

INSTRUMENTS = [
    {"instrument_token": 3329, "exchange_token": 13, "tradingsymbol": "ABB", "name": "ABB INDIA",
     "exchange": "NSE", "segment": "NSE", "instrument_type": "EQ", "tick_size": 0.05, "lot_size": 1},
    {"instrument_token": 408065, "exchange_token": 1594, "tradingsymbol": "INFY", "name": "INFOSYS",
     "exchange": "NSE", "segment": "NSE", "instrument_type": "EQ", "tick_size": 0.05, "lot_size": 1},
    {"instrument_token": 999, "exchange_token": 9, "tradingsymbol": "INFY-BL", "name": "x",
     "exchange": "NSE", "segment": "NSE", "instrument_type": "EQ", "tick_size": 0.05, "lot_size": 1},
    {"instrument_token": 3, "exchange_token": 3, "tradingsymbol": "HFCL-BE", "name": "HFCL",
     "exchange": "NSE", "segment": "NSE", "instrument_type": "EQ", "tick_size": 0.01, "lot_size": 1},
    {"instrument_token": 268041, "exchange_token": 1047, "tradingsymbol": "NIFTY 500", "name": "NIFTY 500",
     "exchange": "NSE", "segment": "INDICES", "instrument_type": "EQ", "tick_size": 0, "lot_size": 0},
    {"instrument_token": 1, "exchange_token": 1, "tradingsymbol": "SOMEBOND", "name": "x",
     "exchange": "NSE", "segment": "NSE", "instrument_type": "BOND", "tick_size": 0.01, "lot_size": 1},
]


class Resp:
    def __init__(self, text, status=200):
        self.text, self.status = text, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


def test_parse_handles_bom_and_columns():
    members = uni.parse_constituents_csv(CSV)
    assert [m["symbol"] for m in members] == ["ABB", "INFY", "GONE", "HFCL", "EMBASSY"]
    assert members[1]["industry"] == "Information Technology" and members[1]["isin"] == "INE009A01021"


def test_parse_rejects_wrong_format():
    with pytest.raises(uni.UniverseError):
        uni.parse_constituents_csv("<html>Access denied</html>")


def test_fetch_falls_through_urls():
    calls = []

    def get(url):
        calls.append(url)
        return Resp("blocked", 403) if len(calls) == 1 else Resp(CSV)
    members, source = uni.fetch_constituents(IngestConfig(), http_get=get)
    assert len(members) == 5 and source == IngestConfig().universe_urls[1]


def test_filter_instruments_keeps_eq_and_indices():
    kept = {r["tradingsymbol"] for r in uni.filter_instruments(INSTRUMENTS)}
    assert kept == {"ABB", "INFY", "INFY-BL", "HFCL-BE", "NIFTY 500"}


def test_refresh_universe_maps_tokens(fake_kite, make_client):
    fake_kite._instruments = INSTRUMENTS
    summary = uni.refresh_universe(make_client(fake_kite), IngestConfig(), http_get=lambda u: Resp(CSV))
    assert summary["members"] == 5 and summary["matched"] == 3
    assert summary["unmatched"] == ["GONE"]
    assert summary["excluded"] == ["EMBASSY"]  # REIT (RR series) not an equity
    assert summary["indices"] == {"NIFTY 500": 268041}
    assert summary["missing_indices"] == ["NIFTY 50"]
