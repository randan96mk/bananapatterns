"""₿ The sweep · reclaim trigger (backend/crypto/trigger.py) and its wiring (signals.process): sweep vs drift (the
approach), the reclaim required, the touches cap per session, the ATR floor, the merge of close levels, the wick stop
and its cap, the session weight, and an alert only when counted + the session alerts + the grade."""

import json
from datetime import datetime, timezone

from backend.crypto import derivs, feed
from backend.crypto import signals as sg
from backend.crypto import trigger as tg
from backend.database.models import get_db
from tests.crypto_world import T_DAY, cfg, k, seed_daily, telegram

T_LON = int(datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc).timestamp())     # Tue 24 Nov 2:30 PM IST (London)
S = 300
PDL = {"kind": "PDL", "name": "PDL", "px": 59000.0, "label": "PDL 59,000 · prev day low", "family": "pd", "start": 0, "end": 0}


def lv(kind, name, v, family):
    return {"kind": kind, "name": name, "px": float(v), "label": f"{name} {v}", "family": family, "start": 0, "end": 0}


def sweep_world(t=T_LON, close=59025.0):
    """21 candles at 59,100 (range 50), 9 easing down to 59,044 (lows ≥ 59,034), then the signal candle: low 58,970
    through 59,000, close back above (a hammer shape)."""
    cs = [k(t - (30 - i) * S, 59100, 59125, 59075, 59100) for i in range(21)]
    cs += [k(t - (9 - i) * S, 59100 - 7 * i, 59130 - 7 * i, 59090 - 7 * i, 59093 - 7 * i) for i in range(9)]
    return cs + [k(t, 59040, 59045, 58970, close, 150)]


def test_sweep_and_reclaim_counted_with_the_shape_as_a_tag():
    c = cfg()
    cs = sweep_world()
    (d,) = tg.sweeps(c, cs, [PDL])
    assert d["pattern"] == "sweep" and d["trigger"] == "sweep" and d["dir"] == "long" and d["counted"] == 1 and d["nc_why"] is None
    assert d["shape"] == "hammer" and d["session"] == "london" and d["approach_atr"] >= 2 and d["approach_n"] >= 9
    assert d["entry"] == 59025 and d["state_set"] == "alert" and d["levels"][0]["name"] == "PDL"


def test_reclaim_required():
    assert tg.sweeps(cfg(), sweep_world(close=58995), [PDL]) == []                # closed under: a break, not a reclaim
    assert tg.sweeps(cfg(), sweep_world(close=59000), [PDL]) == []
    cs = sweep_world()
    cs[-2] = dict(cs[-2], c=58995.0)                                            # it was already under the level: no sweep
    assert tg.sweeps(cfg(), cs, [PDL]) == []


def test_drift_and_no_approach_are_not_counted():
    c = cfg(atr_floor=0)
    drift = [k(T_LON - (30 - i) * S, 59005, 59015, 58990, 59005) for i in range(30)] + [k(T_LON, 59005, 59020, 58980, 59010)]
    (d,) = tg.sweeps(c, drift, [PDL])                                           # hovering along the level: drift (and many touches)
    assert d["counted"] == 0 and d["nc_why"].startswith("drift · touch ") and d["state_set"] == "mark"
    (d,) = tg.sweeps(cfg(atr_floor=0, touches_per_session=0), drift, [PDL])
    assert d["nc_why"] == "drift"
    below = [k(T_LON - (30 - i) * S, 58900, 58925, 58875, 58900) for i in range(29)] + [k(T_LON - S, 58900, 59020, 58895, 59010),
                                                                                       k(T_LON, 59010, 59030, 58985, 59020)]
    (d,) = tg.sweeps(c, below, [PDL])
    assert d["counted"] == 0 and d["nc_why"] == "no approach"


def test_touches_cap_per_session():
    flat = [k(0, 59100, 59125, 59075, 59100)] * 3
    sw = k(0, 59040, 59045, 58970, 59025)
    seq = [k(0, 59100, 59125, 59075, 59100)] * 13 + [sw] + flat + [sw] + flat + [sw]
    cs = [dict(b, t=T_LON - (len(seq) - 1 - i) * S) for i, b in enumerate(seq)]
    (d,) = tg.sweeps(cfg(), cs, [PDL])
    assert d["counted"] == 0 and d["nc_why"] == "touch 3 of the session (first 2)"
    (d,) = tg.sweeps(cfg(), cs[:-4], [PDL])                                     # the second sweep still counts
    assert d["counted"] == 1
    (d,) = tg.sweeps(cfg(touches_per_session=0), cs, [PDL])
    assert d["counted"] == 1


def test_atr_floor():
    cs = sweep_world()
    atr = tg.atr14(cs[:-1])
    (d,) = tg.sweeps(cfg(atr_floor=80), cs, [PDL])
    assert d["counted"] == 0 and d["nc_why"] == f"ATR {sg.px(atr)} < 80"
    assert tg.sweeps(cfg(atr_floor=0), cs, [PDL])[0]["counted"] == 1


def test_close_levels_merge_into_one_detection():
    ds = tg.sweeps(cfg(), sweep_world(), [lv("S1", "S1", 58990, "pivot"), PDL, lv("VWAP", "VWAP", 58900, "vwap")])
    assert len(ds) == 1
    d = ds[0]
    assert d["level_name"] == "PDL + S1" and d["level_px"] == 59000 and tg.merged_text(d["levels"]) == "PDL 59,000 + S1 58,990"


def test_wick_stop_its_cap_and_the_fixed_rule():
    cs = sweep_world()
    atr = tg.atr14(cs[:-1])
    d = tg.sweeps(cfg(), cs, [PDL])[0]                                          # 58,970 − 10 is 65 under: capped at 1 × ATR
    assert d["capped"] and d["stop"] == round(59025 - atr, 2) and d["r1"] == round(59025 + atr, 2)
    d = tg.sweeps(cfg(stop_cap_atr=2.0), cs, [PDL])[0]
    assert not d["capped"] and (d["stop"], d["r1"]) == (58960, 59090)
    d = tg.sweeps(cfg(stop_mode="fixed"), cs, [PDL])[0]                         # the low − stop_pad, at most max_stop (50)
    assert d["capped"] and d["stop"] == 58975
    sr = tg.stop_for(cfg().crypto, "short", {"h": 60030.0, "l": 59960.0, "c": 59975.0}, 100.0)
    assert (sr["entry"], sr["stop"], sr["r1"], sr["capped"]) == (59975, 60040, 59910, False)


def test_session_weight_marks_asia():
    cs = sweep_world(T_DAY)                                                     # 10:00 AM IST
    d = tg.sweeps(cfg(), cs, [PDL])[0]
    assert d["session"] == "asia" and d["counted"] == 0 and d["nc_why"] == "Asia"
    assert tg.sweeps(cfg(session_asia="alerts"), cs, [PDL])[0]["counted"] == 1


# --- through process(): what alerts ------------------------------------------------------------------------------------------

def _store(t):
    seed_daily()                                                                # PDL 59,000 = S1 59,000
    feed.upsert("BTCUSD", "5m", sweep_world(t))


def test_alert_only_when_counted_and_the_session_alerts(monkeypatch):
    tgm = telegram(monkeypatch)
    c = cfg()
    _store(T_LON)
    sg.process(c, T_LON, now_ts=T_LON + 303)
    (r,) = sg.rows("pattern = 'sweep'")
    assert r["state"] == "sent" and r["counted"] == 1 and r["trigger"] == "sweep" and r["shape"] == "hammer" and r["session"] == "london"
    assert r["level_name"] == "PDL + S1" and [x["name"] for x in r["levels_json"]] == ["PDL", "S1"]
    assert sg.rows("pattern = 'hammer'") == []                                  # the pattern is the sweep's tag, not a row
    lines = tgm.sent[0][1].split("\n")
    assert lines[0] == "₿ sweep · reclaim of PDL 59,000 + S1 59,000 · long · 2:30 PM"
    assert lines[1].startswith("approach ") and lines[1].endswith("wick 30 under · close back above · hammer shape")
    assert lines[2].startswith("entry 59,025 · stop ") and "next levels above:" in lines[2]
    assert lines[3] == "derivatives (Delta): no ticker · session London ✓"
    # Asia: a mark, counted 0, never on Telegram
    tgm.sent.clear()
    _store(T_DAY)
    sg.process(c, T_DAY, now_ts=T_DAY + 303)
    a = sg.rows("pattern = 'sweep' AND t = ?", (T_DAY,))[0]
    assert a["state"] == "mark" and a["counted"] == 0 and a["why"] == "Asia" and tgm.sent == []


def test_not_counted_drift_never_alerts_and_touch_patterns_are_marks(monkeypatch):
    tgm = telegram(monkeypatch)
    c = cfg(atr_floor=0, hammer="alert")
    seed_daily()
    cs = [k(T_LON - (30 - i) * S, 59005, 59015, 58990, 59005) for i in range(30)] + [k(T_LON, 59005, 59020, 58980, 59010)]
    feed.upsert("BTCUSD", "5m", cs)
    sg.process(c, T_LON, now_ts=T_LON + 303)
    assert tgm.sent == []
    assert all(r["state"] in ("mark", "dropped") and r["counted"] == 0 for r in sg.rows())
    assert any(r["pattern"] == "sweep" and r["why"].startswith("drift") for r in sg.rows())


def _sweep_record(n=10, won=0, t=T_LON):
    for d in range(1, n + 1):
        det = {"pattern": "sweep", "dir": "long", "t": t - d * 86400, "t0": t - d * 86400, "candles": [k(t, 1, 1, 1, 1)], "level_name": "PDL",
               "level_px": 59000.0, "entry": 59025.0, "stop": 58975.0, "r1": 59075.0, "capped": False, "ema": None, "ema_len": 20, "ema_ok": True,
               "vol_x": 1.5, "vol_ok": True, "trend_ok": True, "day_ok": True, "facts": {}, "trigger": "sweep", "session": "london",
               "levels": [PDL], "counted": 1}
        sid = sg.insert(cfg(), det, "sent", None, None)
        st = "won" if d <= won else "lost"
        with get_db() as conn:
            conn.execute("UPDATE crypto_signals SET outcome_json = ? WHERE id = ?",
                         (json.dumps({"state": st, "r": 1.0 if st == "won" else -1.0, "mins": 10}), sid))


def test_grade_below_the_minimum_is_quiet(monkeypatch):
    tgm = telegram(monkeypatch)
    _sweep_record(10, won=2)
    c = cfg(min_alert_grade="B")
    _store(T_LON)
    sg.process(c, T_LON, now_ts=T_LON + 303)
    r = sg.rows("pattern = 'sweep' AND t = ?", (T_LON,))[0]
    assert r["state"] == "quiet" and r["why"].startswith("grade ") and tgm.sent == []
    assert r["grade_json"]["why"].startswith("sweeps of PDH/PDL/PDC longs in London · 2 of 10 won")


def test_derivatives_in_the_alert_and_the_grade_step(monkeypatch):
    tgm = telegram(monkeypatch)
    _sweep_record(10, won=7)
    for t, oi in ((T_LON - 86400, 1000.0), (T_LON - 1800, 1024.0), (T_LON, 1017.6)):
        derivs.store("BTCUSD", t, {"oi": oi, "funding": 0.012, "mark": 59000.0})
    _store(T_LON)
    sg.process(cfg(), T_LON, now_ts=T_LON + 303)
    r = sg.rows("pattern = 'sweep' AND t = ?", (T_LON,))[0]
    assert r["derivs_json"] == {"oi_30m_pct": -0.62, "oi_24h_pct": 1.76, "funding": 0.012, "oi_state": "flushed", "funding_state": "neutral"}
    assert r["grade_json"]["derivs_step"] == {"oi": 1, "funding": 0} and "OI flushed +1" in r["grade_json"]["why"]
    text = tgm.sent[0][1]
    assert "derivatives (Delta): OI −0.6 % in 30 min (longs flushed) ✓ · funding +0.012 % neutral · session London ✓" in text
    assert "\nrecord: sweeps of PDH/PDL/PDC longs in London · 7 of 10 won" in text


def test_history_scan_uses_the_sweep_and_sets_counted(monkeypatch):
    tgm = telegram(monkeypatch)
    _store(T_LON)
    _store(T_DAY)
    out = sg.scan_history(cfg(), days=1, now_ts=T_LON + 3600)
    assert out["marked"] >= 1 and tgm.sent == []
    lon = sg.rows("pattern = 'sweep' AND dir = 'long' AND t = ?", (T_LON,))[0]
    asia = sg.rows("pattern = 'sweep' AND dir = 'long' AND t = ?", (T_DAY,))[0]
    assert (lon["state"], lon["counted"], lon["why"]) == ("mark", 1, "history scan")
    assert (asia["state"], asia["counted"], asia["why"]) == ("mark", 0, "history scan · Asia")
    assert sg.scan_history(cfg(), days=1, now_ts=T_LON + 3600)["marked"] == 0           # idempotent
