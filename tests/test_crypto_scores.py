"""₿ Outcomes, the grade, forecast candidates in shadow, the cone (backend/crypto/outcomes.py, grade.py, forecast.py,
snapshot.py): won / lost / neither / both in one candle / the backfill, each grade letter, too few, the model step only
behind its gate, the forecast checks (odds, ATR move, pivot band, EMA, hours, cooldown, shadow vs send), the PNG with and
without the cone, the payload keys, min_alert_grade quieting, and nothing in the alert path waiting on the worker."""

import json
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.config import IST, CryptoConfig
from backend.crypto import feed, forecast, outcomes, view
from backend.crypto import grade as gd
from backend.crypto import signals as sg
from backend.database.models import get_db
from tests.crypto_world import TOUCH, DAY1, T_DAY, T_NIGHT, FakeDelta, cfg, flat, hammer_world, k, seed_daily, telegram
from tests.test_crypto_api import api  # noqa: F401  (the API fixture)

QUIET = dict(vol_len=20, dragonfly_doji="off", tweezer_bottom="off", **TOUCH)   # the old rule, Asia alerting
S = 300


def _row(t, pattern="hammer", d="long", level="S1", entry=59100.0, stop=59050.0, r1=59150.0, state="mark", outcome=None, kind="pattern",
         fc=None, c=None) -> int:
    det = {"pattern": pattern, "dir": d, "t": t, "t0": t, "candles": [k(t, entry, entry, entry, entry)], "level_name": level, "level_px": 59000.0,
           "entry": entry, "stop": stop, "r1": r1, "capped": False, "ema": None, "ema_len": 20, "ema_ok": True, "vol_x": 1.3, "vol_ok": True,
           "trend_ok": True, "day_ok": True, "facts": {}}
    sid = sg.insert(c or cfg(), det, state, None, None, kind=kind, fc=fc)
    if outcome:
        r = {"won": 1.0, "lost": -1.0, "neither": 0.2}[outcome]
        with get_db() as conn:
            conn.execute("UPDATE crypto_signals SET outcome_json = ? WHERE id = ?",
                         (json.dumps({"state": outcome, "r": r, "mins": 10, "px_at": entry, "scored_at": "x"}), sid))
    return sid


# --- outcomes ---------------------------------------------------------------------------------------------------------------

def _sig(d="long"):
    return {"t": T_DAY, "dir": d, "entry": 59100.0, "stop": 59050.0 if d == "long" else 59150.0, "r1": 59150.0 if d == "long" else 59050.0}


def test_outcome_won_lost_neither_and_both_in_one_candle():
    after = [k(T_DAY + S * i, 59100, 59120, 59080, 59100) for i in range(1, 13)]
    end = T_DAY + S + 3600
    assert outcomes.score_one(_sig(), after, S, 60, now_ts=end - 1) is None                  # not due yet
    nei = outcomes.score_one(_sig(), after[:-1] + [k(T_DAY + S * 12, 59100, 59120, 59080, 59110)], S, 60, now_ts=end)
    assert (nei["state"], nei["r"], nei["mins"], nei["px_at"]) == ("neither", 0.2, 60, 59110)
    won = list(after)
    won[3] = k(T_DAY + S * 4, 59100, 59155, 59080, 59140)                                     # 1R in the 4th candle
    o = outcomes.score_one(_sig(), won, S, 60, now_ts=end)
    assert (o["state"], o["r"], o["mins"], o["px_at"]) == ("won", 1.0, 20, 59150.0)
    lost = list(after)
    lost[1] = k(T_DAY + S * 2, 59100, 59120, 59040, 59060)
    o = outcomes.score_one(_sig(), lost, S, 60, now_ts=end)
    assert (o["state"], o["r"], o["mins"]) == ("lost", -1.0, 10)
    both = list(after)
    both[0] = k(T_DAY + S, 59100, 59160, 59040, 59100)                                       # stop and 1R in one candle: lost
    assert outcomes.score_one(_sig(), both, S, 60, now_ts=end)["state"] == "lost"
    short = list(after)
    short[2] = k(T_DAY + S * 3, 59100, 59120, 59045, 59060)                                  # a short: the low reaches 1R
    o = outcomes.score_one(_sig("short"), short, S, 60, now_ts=end)
    assert (o["state"], o["mins"]) == ("won", 15)
    assert outcomes.score_one(_sig(), [], S, 60, now_ts=end) is None                         # no candles: wait


def test_score_due_stores_once_never_rescored_and_scores_the_model():
    c = cfg()
    feed.upsert("BTCUSD", "5m", flat(T_DAY) + [k(T_DAY, 59100, 59100, 59100, 59100)] +
                [k(T_DAY + S * i, 59100, 59155 if i == 2 else 59120, 59080, 59130) for i in range(1, 25)])
    fc = {"p_up": 0.62, "touch_r1": 0.5, "touch_stop": 0.3, "horizon_min": 30, "last": 59100.0}
    sid = _row(T_DAY, state="sent", fc=fc)
    fsid = _row(T_DAY, pattern="forecast 66 % down", d="short", entry=59100, stop=59150, r1=59050, state="quiet", kind="forecast")
    assert outcomes.score_due(c, now_ts=T_DAY + 600)["scored"] == 0                          # not due
    out = outcomes.score_due(c, now_ts=T_DAY + S + 3600)
    assert out == {"scored": 2, "model": 1}
    r = sg.load(sid)
    assert r["outcome_json"]["state"] == "won" and r["outcome_json"]["mins"] == 10
    assert r["fc_json"]["right"] is True and r["fc_json"]["actual_px"] == 59130             # up, and the close 30 min later is higher
    assert sg.load(fsid)["outcome_json"]["state"] == "lost"            # a short: the 59,155 high took the stop
    with get_db() as conn:
        conn.execute("UPDATE crypto_signals SET outcome_json = ? WHERE id = ?", (json.dumps({"state": "lost", "r": -1, "mins": 5}), sid))
    assert outcomes.score_due(c, now_ts=T_DAY + 7200) == {"scored": 0, "model": 0}            # never rescored
    assert sg.load(sid)["outcome_json"]["state"] == "lost"
    c.crypto.score_after_min = 30
    assert sg.rows() and all(x["outcome_json"] for x in sg.rows())


def test_backfill_on_start_scores_and_grades_the_older_rows(monkeypatch):
    c = cfg(history_days=1, scan_days=0)
    now = T_DAY + 3 * 3600 + 3
    series = {"5m": [k(t, 59000, 59010, 58990, 59000) for t in range(T_DAY - 86400, now - 300, 300)],
              "1d": [k(DAY1 - 86400 * i, 1, 2, 0, 1) for i in range(5)]}
    ids = [_row(T_DAY - 3600 * i, entry=59000, stop=58995, r1=59005) for i in (1, 2, 3)]   # the "190 detections": no outcome, no grade
    monkeypatch.setattr(sg, "process", lambda config, t, now_ts=None, candles=None: [])
    svc = feed.CryptoService(SimpleNamespace(config=c), client=feed.DeltaClient(get=FakeDelta(series), sleep=lambda s: None), clock=lambda: now)
    svc.tick()
    for i in ids:
        r = sg.load(i)
        assert r["outcome_json"]["state"] == "lost" and r["outcome_json"]["mins"] == 5    # 58,990 ≤ 58,995 and 59,010 ≥ 59,005: both, lost
        assert r["grade_json"]["few"] is True and r["grade"] is None


# --- the grade ----------------------------------------------------------------------------------------------------------------

def test_letters_and_shift_and_below():
    assert gd.letter(60, 0.1, 3) == "A" and gd.letter(60, 0, 3) == "B" and gd.letter(70, 0.5, 2) == "B"
    assert gd.letter(50, -0.2, 1) == "C" and gd.letter(40, -1, 0) == "C" and gd.letter(39, -1, 0) == "D"
    assert gd.shift("C", 1) == "B" and gd.shift("A", 1) == "A" and gd.shift("D", -1) == "D" and gd.shift(None, 1) is None
    assert gd.below("C", "B") and not gd.below("B", "B") and not gd.below("D", "all") and not gd.below(None, "A")
    assert gd.family("tweezer_top") == gd.family("bearish_engulfing") == "engulf" and gd.family("forecast 66 % down") == "forecast"
    assert gd.level_kind("R2") == "R" and gd.level_kind("S1") == "S" and gd.level_kind("P") == "P"


def _record(won: int, n: int = 10, r=None):
    for d in range(1, n + 1):
        _row(T_DAY - d * 86400, outcome="won" if d <= won else "lost")


DET = {"pattern": "hammer", "dir": "long", "t": T_DAY, "level_name": "S1", "entry": 59105.0, "ema_ok": True, "vol_x": 1.3}
LEVELS = {"S1": 59000.0, "P": 60000.0, "R1": 61000.0}


@pytest.mark.parametrize("won,ctx,g", [(7, "all", "A"), (5, "two", "B"), (4, "none", "C"), (3, "one", "C"), (3, "none", "D")])
def test_each_grade_letter(won, ctx, g):
    _record(won)
    det = dict(DET)
    if ctx in ("two", "one", "none"):
        det["vol_x"] = 1.0
    if ctx in ("one", "none"):
        det["ema_ok"] = False
    atr = None if ctx == "none" else 20.0
    gj = gd.grade_for(cfg(), det, T_DAY + 600, levels=LEVELS, atr=atr)
    assert gj["g"] == g and gj["n"] == 10 and gj["few"] is False
    assert gj["why"].startswith("hammer/dragonfly longs at S levels 8:00 AM–4:00 PM") and f"{won} of 10 won" in gj["why"]


def test_too_few_and_other_buckets_do_not_count():
    _record(7, n=9)
    _row(T_DAY - 86400 + 10 * 3600, outcome="won")                     # another session bucket (after 4 PM)
    _row(T_DAY - 86400, pattern="shooting_star", d="short", level="R1", outcome="won")
    gj = gd.grade_for(cfg(), DET, T_DAY + 600, levels=LEVELS, atr=20.0)
    assert gj["g"] is None and gj["few"] is True and gj["why"] == "too few like this (9)"


def test_model_step_applied_only_behind_the_gate(monkeypatch):
    _record(5)
    up = {"touch_r1": 0.6, "touch_stop": 0.3}
    gj = gd.grade_for(cfg(), DET, T_DAY + 600, levels=LEVELS, atr=20.0, fc=up, gate_ok=False)
    assert gj["g"] == "A" or gj["g"] == "B"
    base = gj["g"]
    assert gj["model_step"] == 1 and gj["model_applied"] is False and gj["why"].endswith("shadow +1")
    gj2 = gd.grade_for(cfg(), DET, T_DAY + 600, levels=LEVELS, atr=20.0, fc={"touch_r1": 0.2, "touch_stop": 0.5}, gate_ok=True)
    assert gj2["model_step"] == -1 and gj2["model_applied"] is True and gj2["g"] == gd.shift(base, -1) and gj2["why"].endswith("model -1")
    assert gd.model_step({"touch_r1": 0.5, "touch_stop": 0.4}) == 0 and gd.model_step(None) is None
    monkeypatch.setattr(outcomes, "MODEL_GATE_N", 2)                   # the gate reads the BTC scorecard by default
    assert outcomes.fc_gate_ok(cfg()) is False
    _row(T_DAY, fc={"p_up": 0.6, "right": True})
    _row(T_DAY, fc={"p_up": 0.6, "right": True})
    assert outcomes.model_card(cfg()) == {"n": 2, "hit": 2, "pct": 100, "ok": True}


# --- min_alert_grade, the alert lines --------------------------------------------------------------------------------------

def _hammer_record(won=3):
    for d in range(1, 11):
        _row(T_DAY - d * 86400, outcome="won" if d <= won else "lost")


def test_min_alert_grade_quiets_and_the_grade_lines(monkeypatch):
    tg = telegram(monkeypatch)
    _hammer_record(3)
    c = cfg(min_alert_grade="B", **QUIET)
    hammer_world()
    sg.process(c, T_DAY, now_ts=T_DAY + 303)
    r = sg.rows("pattern = 'hammer' AND t = ?", (T_DAY,))[0]
    assert r["state"] == "quiet" and r["why"] == "grade C < min B" and r["grade"] == "C" and tg.sent == []
    assert sg.digest_if_due(c, now_ts=T_DAY + 900) == 0                     # not a quiet-hours row: no digest
    v = view.row_view(r)
    assert v["grade"]["g"] == "C" and "3 of 10 won" in v["grade"]["why"] and v["kind"] == "pattern"


def test_grade_in_the_alert_head_and_the_record_line(monkeypatch):
    tg = telegram(monkeypatch)
    _hammer_record(7)
    c = cfg(**QUIET)
    hammer_world()
    sg.process(c, T_DAY, now_ts=T_DAY + 303)
    head, rec = tg.sent[0][1].split("\n")
    assert head.startswith("₿ hammer on S1 59,000 · 5 min · 10:00 AM · long bias · grade A · stop under 59,055")
    assert rec.startswith("record: hammer/dragonfly longs at S levels 8:00 AM–4:00 PM · 7 of 10 won · +0.4 R avg · context 3/3") and rec.endswith("→ grade A")


# --- the forecast candidates ---------------------------------------------------------------------------------------------

def _down_world(t=T_DAY, n=40, top=61040.0):
    """n candles stepping down 1 point a candle to just under R1 61,000 (close < EMA 20, ATR ≈ 21)."""
    return [k(t - (n - 1 - i) * S, top - i, top - i + 10, top - i - 10, top - i - 1) for i in range(n)]


def fake_worker(p_up=0.3, exp_range=30.0, sleep=0.0, calls=None):
    def w(payload):
        if calls is not None:
            calls.append(payload)
        if sleep:
            time.sleep(sleep)
        h = payload["horizon"]
        last = payload["candles"][-1]["c"]
        touch = {f"{float(x):g}": round(0.2 + 0.1 * i, 2) for i, x in enumerate(payload["levels"])}
        ps = {q: [round(last + d * (j + 1), 2) for j in range(h)] for q, d in (("q10", -8), ("q25", -5), ("q50", -2), ("q75", 1), ("q90", 4))}
        one = {"horizon": h, "p_up": p_up, "exp_range": exp_range, "paths_summary": ps, "touch": touch, "samples": 10}
        return {**one, "reads": [one], "ms": 5}
    return w


def test_forecast_checks():
    c = cfg()
    cs = _down_world()
    now = datetime.fromtimestamp(T_DAY + 303, IST)
    ok = forecast.evaluate(c, cs, LEVELS, {"p_up": 0.3, "exp_range": 30}, now)
    assert ok["ok"] and ok["dir"] == "short" and ok["pivot"] == ("R1", 61000.0) and ok["p"] == 0.7
    assert ok["stop"] == min(cs[-1]["c"] + ok["atr"], 61010.0) and ok["r1"] == round(2 * cs[-1]["c"] - ok["stop"], 2)
    assert forecast.evaluate(c, cs, LEVELS, {"p_up": 0.4, "exp_range": 30}, now)["why"] == "odds 60 % < 62 %"
    c.crypto.forecast_p = 58
    assert forecast.evaluate(c, cs, LEVELS, {"p_up": 0.4, "exp_range": 30}, now)["ok"]
    assert forecast.evaluate(c, cs, LEVELS, {"p_up": 0.3, "exp_range": 5}, now)["why"].startswith("move ±5 < 0.6 × ATR")
    c.crypto.forecast_move_atr = 1.0
    assert not forecast.evaluate(c, cs, LEVELS, {"p_up": 0.3, "exp_range": 15}, now)["ok"]
    assert forecast.evaluate(c, _down_world(top=61200), LEVELS, {"p_up": 0.3, "exp_range": 30}, now)["why"] == "no pivot within 0.6 × ATR"
    assert forecast.evaluate(c, cs, LEVELS, {"p_up": 0.8, "exp_range": 30}, now)["why"] == "EMA 20 side disagrees"
    assert forecast.evaluate(c, cs, LEVELS, {"p_up": 0.3, "exp_range": 30}, datetime.fromtimestamp(T_NIGHT, IST))["why"] == "outside the alert hours"


def test_forecast_shadow_cooldown_and_the_send_gate(monkeypatch):
    from backend.market import reads as rd
    tg = telegram(monkeypatch)
    seed_daily()
    calls = []
    monkeypatch.setattr(rd, "WORKER", fake_worker(calls=calls))
    c = cfg()
    assert forecast.step(c, T_DAY, _down_world(), T_DAY + 303) is None                     # the candle model off: nothing
    c.market.kronos = True
    sid = forecast.step(c, T_DAY, _down_world(), T_DAY + 303)
    r = sg.load(sid)
    assert r["kind"] == "forecast" and r["pattern"] == "forecast 70 % down" and r["state"] == "quiet" and r["why"] == "shadow · not sent"
    assert r["level_name"] == "R1" and r["entry"] == 61000.0 and r["fc_json"]["p_down"] == 0.7 and r["fc_json"]["paths"]["p25"]
    assert r["fc_json"]["touch_stop"] == 0.2 and r["fc_json"]["touch_r1"] == 0.3                 # the second read: touch = stop, 1R
    assert calls[1]["levels"] == [r["stop"], r["r1"]] and tg.sent == []
    assert forecast.step(c, T_DAY, _down_world(), T_DAY + 303) is None                     # once per candle
    assert forecast.step(c, T_DAY + S, _down_world(T_DAY + S), T_DAY + 603) is None         # the cooldown on R1 short
    v = view.row_view(r)
    assert v["kind"] == "forecast" and v["forecast"]["chip"] == "70 % down · ±30" and v["forecast"]["dir"] == "short"
    c.crypto.forecast_alerts = "send"                                                       # send without the gate: still shadow
    t2 = T_DAY + 3600
    r2 = sg.load(forecast.step(c, t2, _down_world(t2), t2 + 303))
    assert r2["state"] == "quiet" and r2["why"] == "shadow · held in shadow: needs 40 scored and 55 %"
    for i in range(40):                                                                      # the gate: 40 scored, 60 % won
        _row(T_DAY - 86400 - i * 600, pattern="forecast 66 % down", d="short", kind="forecast", state="quiet",
             outcome="won" if i < 24 else "lost", fc={"p_up": 0.34, "p": 0.66})
    assert outcomes.forecast_card(c)["ok"] is True
    t3 = T_DAY + 7200
    r3 = sg.load(forecast.step(c, t3, _down_world(t3), t3 + 303))
    assert r3["state"] == "sent" and r3["sent_at"] and tg.sent[-1][0] == "photo"
    head, model = tg.sent[-1][1].split("\n")[:2]
    assert head.startswith("₿ forecast 70 % down near R1 61,000 · 5 min · 12:00 PM · short bias")
    assert model.startswith("📈 candle model (shadow): 70 % down · ") and "in 30 min · stop first 20 % · 1R first 30 %" in model
    sc = outcomes.scores(c, 30, now_ts=T_DAY + 8000)
    assert sc["forecast"]["scored"] == 40 and sc["forecast"]["pct"] == 60 and sc["forecast"]["gate"]["ok"] is True
    assert next(b for b in sc["forecast"]["buckets"] if b["lo"] == 66)["n"] == 40


def test_worker_down_skips_silently(monkeypatch):
    from backend.market import reads as rd
    seed_daily()
    monkeypatch.setattr(rd, "WORKER", lambda p: None)
    c = cfg()
    c.market.kronos = True
    assert forecast.step(c, T_DAY, _down_world(), T_DAY + 303) is None and sg.rows() == []


# --- the cone ---------------------------------------------------------------------------------------------------------------

def test_cone_png_with_and_without_paths(tmp_path):
    from backend.crypto import snapshot
    cs = _down_world()
    r = {"id": 1, "t": T_DAY, "dir": "short", "entry": 60960.0, "stop": 60981.0, "r1": 60939.0, "level_name": "R1",
         "facts_json": {"levels": LEVELS, "candles": [cs[-1]]}}
    h = 6
    paths = {"median": [60955 - j for j in range(h)], "p25": [60950 - j for j in range(h)], "p75": [60962 - j for j in range(h)],
             "p10": [60940 - j for j in range(h)], "p90": [60970 - j for j in range(h)]}
    a = snapshot.render(r, cs, "cap", tmp_path / "a.png")
    b = snapshot.render(r, cs, "cap", tmp_path / "b.png", paths=paths)
    assert a.read_bytes()[:4] == b.read_bytes()[:4] == b"\x89PNG" and a.read_bytes() != b.read_bytes()
    assert snapshot.cone_of({"median": [1, 2], "p25": [None, 1]}) == {"median": [1.0, 2.0]}
    assert snapshot.cone_of(None) is None


def test_the_cone_lands_on_the_png_when_the_read_arrives(monkeypatch):
    from backend.market import reads as rd
    telegram(monkeypatch)
    monkeypatch.setattr(rd, "WORKER", fake_worker(p_up=0.6))
    c = cfg(**QUIET)
    c.market.kronos = True
    hammer_world()
    sg.process(c, T_DAY, now_ts=T_DAY + 303)
    forecast.drain()
    r = sg.rows("pattern = 'hammer'")[0]
    assert r["fc_json"]["cone_png"] is True and r["fc_json"]["paths"]["median"] and r["png_path"].endswith(f"/{r['id']}.png")
    c.crypto.forecast_cone = False
    sid = _row(T_DAY + 600, state="mark")
    assert forecast.signal_read(c, sid, hammer_world(store=False)) is not None and "cone_png" not in sg.load(sid)["fc_json"]


# --- nothing waits on the worker -------------------------------------------------------------------------------------------

def test_the_alert_never_waits_on_a_slow_worker(monkeypatch):
    """A 5 s candle-model worker: the service's alert goes within 1 s; the read lands afterwards."""
    from backend.market import reads as rd
    tg = telegram(monkeypatch)
    sent_at = []
    real = tg.send_photo
    monkeypatch.setattr(tg, "send_photo", lambda *a, **kw: sent_at.append(time.monotonic()) or real(*a, **kw))
    monkeypatch.setattr(rd, "WORKER", fake_worker(p_up=0.5, sleep=5.0))
    c = cfg(history_days=1, scan_days=0, **QUIET)
    c.market.kronos = True
    cs = hammer_world(store=False)
    seed_daily()
    series = {"5m": cs, "1d": [k(DAY1 - 86400 * i, 59050, 61000, 59000, 60000) for i in range(-1, 4)]}
    svc = feed.CryptoService(SimpleNamespace(config=c), client=feed.DeltaClient(get=FakeDelta(series), sleep=lambda s: None),
                             clock=lambda: T_DAY + 303)
    t0 = time.monotonic()
    svc.tick()
    assert sent_at and sent_at[0] - t0 < 1.0, "the alert waited on the candle model"
    r = sg.rows("pattern = 'hammer'")[0]
    assert r["state"] == "sent" and r["sent_at"]
    forecast.drain(20)
    assert sg.load(r["id"])["fc_json"]["p_up"] == 0.5


# --- the payload -----------------------------------------------------------------------------------------------------------

def test_settings_defaults_and_validation():
    d = CryptoConfig()
    assert (d.score_after_min, d.min_alert_grade, d.forecast_alerts, d.forecast_p, d.forecast_move_atr, d.forecast_cone) == \
        (60, "all", "shadow", 62, 0.6, True)
    c = cfg()
    for bad in ({"score_after_min": 45}, {"min_alert_grade": "E"}, {"forecast_alerts": "maybe"}, {"forecast_p": 70}, {"forecast_move_atr": 0.8}):
        with pytest.raises(ValueError):
            c.update_section("crypto", bad)
    assert c.update_section("crypto", {"score_after_min": 120, "min_alert_grade": "B", "forecast_p": 66, "forecast_move_atr": 1})["forecast_move_atr"] == 1.0


def test_payload_keys(api, monkeypatch):  # noqa: F811
    telegram(monkeypatch)
    c = cfg(vol_len=20, **TOUCH)
    hammer_world()
    sg.process(c, T_DAY, now_ts=T_DAY + 303)
    sid = _row(T_DAY, pattern="forecast 66 % down", d="short", kind="forecast", state="quiet",
               fc={"p_up": 0.34, "exp_range": 150, "horizon_min": 30, "paths": {"median": [1, 2], "p25": [1, 2], "p75": [1, 2], "p10": [1, 2], "p90": [1, 2]}})
    j = api.get("/api/crypto").json()
    sc = j["scores"]
    assert {"days", "direction", "by_grade", "forecast", "by_pattern", "model_chip"} <= set(sc) and sc["days"] == 30
    assert set(sc["direction"]) == {"long", "short"} and set(sc["by_grade"]) == {"A", "B", "C", "D"}
    assert {"scored", "won", "pct", "buckets", "gate"} <= set(sc["forecast"]) and sc["forecast"]["gate"]["why"] == "held in shadow: needs 40 scored and 55 %"
    s = next(x for x in j["signals"] if x["pattern"] == "hammer")
    assert {"kind", "grade", "forecast", "outcome"} <= set(s) and s["kind"] == "pattern"
    assert set(s["grade"]) == {"g", "why", "n", "few", "model_step"} and s["outcome"]["state"] == "open" and s["outcome"]["tone"] == "dim"
    f = next(x for x in j["signals"] if x["id"] == sid)
    assert f["kind"] == "forecast" and f["forecast"]["chip"] == "66 % down · ±150" and f["forecast"]["p"] == 0.66
    assert api.get("/api/crypto/scores?days=7").json()["days"] == 7
    assert api.get("/api/crypto/scores?days=5").status_code == 400
    ch = api.get(f"/api/crypto/candles?tf=5m&from={T_DAY - 7200}&to={T_DAY + 300}").json()
    fs = next(x for x in ch["signals"] if x["id"] == sid)["forecast"]
    assert fs["paths"]["median"] == [1, 2] and fs["times"] == [T_DAY + 300 + 19800, T_DAY + 600 + 19800] and fs["t0"] == T_DAY + 19800
    r = api.put("/api/config/crypto", json={"min_alert_grade": "B", "forecast_alerts": "send", "forecast_cone": False})
    assert r.status_code == 200 and r.json()["min_alert_grade"] == "B" and r.json()["forecast_cone"] is False
    assert api.put("/api/config/crypto", json={"forecast_p": 50}).status_code == 422


def test_outcome_chips():
    base = {"t": T_DAY, "tf": "5m"}
    assert view.outcome_view({**base, "outcome_json": {"state": "won", "r": 1.0, "mins": 20}})["chip"] == "✓ 1R hit · 20 min · +1.0 R"
    assert view.outcome_view({**base, "outcome_json": {"state": "lost", "r": -1.0, "mins": 10}}) == \
        {"state": "lost", "chip": "✗ stop · 10 min · −1.0 R", "r": -1.0, "mins": 10, "tone": "down"}
    assert view.outcome_view({**base, "outcome_json": {"state": "neither", "r": 0.3, "mins": 60}})["chip"] == "neither · +0.3 R at 60 min"
    o = view.outcome_view(base, 60, now_ts=T_DAY + 300 + 53 * 60)
    assert o == {"state": "open", "chip": "open · 7 min to score", "r": None, "mins": None, "tone": "dim"}
