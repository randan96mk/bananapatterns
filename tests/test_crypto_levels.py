"""₿ The levels that matter (backend/crypto/levels.py) from a synthetic day: PDH / PDL / PDC, the session highs / lows,
the day's VWAP ±1σ, the 24 h volume profile (POC / VAH / VAL), the pivots; session_of around midnight IST."""

from datetime import datetime, timezone

import pytest

from backend.config import IST
from backend.crypto import feed
from backend.crypto import levels as lvm
from tests.crypto_world import cfg, k

UTC = timezone.utc
MON = int(datetime(2026, 11, 23, tzinfo=UTC).timestamp())          # Mon 23 Nov 00:00 UTC (5:30 AM IST: Asia opens)
TUE = MON + 86400


def _world():
    """Mon (the previous UTC day): flat 60,000 ± 10, the high 60,500 in London (10:00 UTC), the low 59,500 in New York
    (15:00 UTC), the last close 60,100. Tue: 36 candles 60,050 ± 20 to 3:00 UTC (8:30 AM IST, Asia running)."""
    cs = []
    for t in range(MON, TUE, 300):
        h, low, c = 60010.0, 59990.0, 60000.0
        if t == MON + 10 * 3600:
            h = 60500.0
        if t == MON + 15 * 3600:
            low = 59500.0
        if t == TUE - 300:
            c = 60100.0
        cs.append(k(t, 60000, h, low, c, 10))
    cs += [k(t, 60050, 60070, 60030, 60050, 20) for t in range(TUE, TUE + 3 * 3600, 300)]
    feed.upsert("BTCUSD", "5m", cs)
    return cs


def _by(ls):
    return {x["kind"] + (f":{x['session']}" if x.get("session") else ""): x for x in ls}


def test_pd_session_vwap_profile_and_pivots_from_a_synthetic_day():
    cs = _world()
    t = TUE + 3 * 3600
    ls = lvm.levels_at(cfg(), t)
    b = _by(ls)
    assert (b["PDH"]["px"], b["PDL"]["px"], b["PDC"]["px"]) == (60500, 59500, 60100)
    assert b["PDL"]["label"] == "PDL 59,500 · prev day low" and b["PDL"]["family"] == "pd"
    assert b["PDL"]["start"] == TUE and b["PDL"]["end"] == TUE + 86400
    # the sessions: Asia running (Tue), the finished New York (Mon 18:30 IST →) and London (Mon)
    assert (b["SES_H:asia"]["px"], b["SES_L:asia"]["px"]) == (60070, 60030) and "running" in b["SES_H:asia"]["label"]
    assert b["SES_H:london"]["px"] == 60500 and b["SES_H:london"]["name"] == "London H"
    assert b["SES_L:ny"]["px"] == 59500 and b["SES_L:ny"]["label"] == "NY L 59,500 · New York low"
    # VWAP: the day's candles only (all 60,050 typical → σ 0, no ±1σ lines)
    assert b["VWAP"]["px"] == pytest.approx(60050) and "VWAP+1" not in b
    # the profile of the last 24 h
    pr = lvm.profile([x for x in cs if t - 86400 <= x["t"] < t])
    assert (b["POC"]["px"], b["VAH"]["px"], b["VAL"]["px"]) == (pr["poc"], pr["vah"], pr["val"])
    assert b["VAL"]["px"] <= b["POC"]["px"] <= b["VAH"]["px"]
    # families switch off
    c = cfg(levels_profile=False, levels_sessions=False, levels_pivots=False)
    assert {x["family"] for x in lvm.levels_at(c, t)} == {"pd", "vwap"}


def test_a_candle_never_makes_its_own_level():
    _world()
    t = TUE + 3 * 3600
    feed.upsert("BTCUSD", "5m", [k(t, 60050, 60900, 59000, 60050, 20)])
    b = _by(lvm.levels_at(cfg(), t))
    assert b["SES_H:asia"]["px"] == 60070 and b["SES_L:asia"]["px"] == 60030


def test_vwap_and_sigma():
    bars = [k(0, 100, 100, 100, 100, 1), k(300, 110, 110, 110, 110, 3)]
    v = lvm.vwap(bars)
    assert v["vwap"] == 107.5 and v["sd"] == pytest.approx(4.33, abs=0.01)
    s = lvm.vwap_series(bars, 0, 600)
    assert [p["vwap"] for p in s] == [100, 107.5] and s[-1]["up"] == pytest.approx(111.83, abs=0.01)
    assert lvm.vwap([k(0, 1, 1, 1, 1, 0)]) is None


def test_profile_poc_and_value_area():
    bars = [k(0, 110, 119, 100, 110, 100), k(300, 130, 139, 120, 130, 50), k(600, 90, 99, 80, 90, 30)]
    assert lvm.profile(bars) == {"poc": 110, "vah": 140, "val": 100}


@pytest.mark.parametrize("hm,want", [((23, 59), "ny"), ((0, 1), "ny"), ((3, 29), "ny"), ((5, 29), "ny"), ((5, 30), "asia"),
                                     ((13, 29), "asia"), ((13, 30), "london"), ((18, 29), "london"), ((18, 30), "ny")])
def test_session_of_around_midnight_ist(hm, want):
    ts = datetime(2026, 11, 24, *hm, tzinfo=IST).timestamp()
    assert lvm.session_of(ts) == want


def test_session_span_after_midnight_belongs_to_the_evening_before():
    s = lvm.session_span(datetime(2026, 11, 25, 0, 30, tzinfo=IST).timestamp())
    assert s["key"] == "ny" and s["start"] == datetime(2026, 11, 24, 18, 30, tzinfo=IST).timestamp()
    assert s["end"] == datetime(2026, 11, 25, 3, 30, tzinfo=IST).timestamp()
    info = lvm.sessions_info(datetime(2026, 11, 24, 15, 0, tzinfo=IST).timestamp())
    assert info["current"] == "london" and [w["key"] for w in info["windows"]] == ["asia", "london", "ny"]
    assert info["windows"][2]["from"] == "6:30 PM" and info["windows"][2]["until"] == "3:30 AM"
