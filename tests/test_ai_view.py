"""🤖 AI view: the facts code computes (SMA stack, ATR, dry-up, results date, industry move), the candidate levels and
the model's pick validation, the verdict recomputed by code, the held exit-watch on a broken fails_below, the previous
view and the change, the calibration note, the nightly order and the cap, the outcomes at 5 / 10 / 20, the shuffle test,
the record and the unlock rule, the locked switches (400), the size-by-score math (never above 1.0×, never a skip, never
a stop change, `full SYMBOL`), rank ties, the exit-watch alert, the API shapes and the `ai_view` joins, the Telegram
lines. Fake Ollama; no network."""

import json
import math
import random
import re
from datetime import date, datetime, timedelta

import pytest

from backend.ai import client, control, guard, quality, view
from backend.ai import queue as q
from backend.alerts import messages as msg
from backend.alerts import plan as pl
from backend.alerts.dispatch import Notifier
from backend.config import IST, AIConfig, AlertsConfig, AppConfig
from backend.database.models import get_db, now_ist
from backend.engine import real
from tests.test_ai_infra import no_brokers
from tests.test_ai_notes import FakeNotes
from tests.test_alerts import FakeTG
from tests.test_login import BotTG, LoginState, update

NIGHT = datetime(2026, 11, 17, 21, 0, tzinfo=IST)        # a Tuesday evening
DAY = "2026-11-17"


# --- fixtures and seeds ---------------------------------------------------------------------------------------------------

class Model:
    """A fake model that reads the prompt like a model would: the close, the candidates; it picks the highest
    candidate under the close as support_1 / fails_below and the lowest over it as resistance."""

    def __init__(self, score=72, verdict="buy", reasons=None, change=None, bad=None):
        self.score, self.verdict, self.reasons, self.change, self.bad = score, verdict, reasons, change, bad

    def __call__(self, body):
        p = body["prompt"]
        if isinstance(self.bad, list) and self.bad:
            return self.bad.pop(0)
        return self.bad if isinstance(self.bad, str) else self._good(p)

    def _good(self, p):
        close = float(re.search(r"^- close: ([\d.]+)", p, re.M).group(1))
        block = p.split("Candidate levels (pick only from these prices):\n")[1]
        prices = [float(m) for m in re.findall(r"^- ([\d.]+) · ", block, re.M)]
        under = sorted([x for x in prices if x < close], reverse=True)
        over = sorted(x for x in prices if x > close)
        verdict = self.verdict if "held name" not in p else ("hold" if self.verdict == "buy" else self.verdict)
        return json.dumps({"score": self.score, "verdict": verdict, "confidence": 0.7, "reasons": self.reasons or ["base is tight", "volume dried up"],
                           "change": self.change, "levels": {"support_1": under[0], "support_2": under[1] if len(under) > 1 else None,
                                                             "resistance": over[0] if over else None, "fails_below": under[0]},
                           "would_change": "a close under the support"})


@pytest.fixture
def model():
    return Model()


@pytest.fixture
def ollama(monkeypatch, model):
    fake = FakeNotes(reply=model)
    monkeypatch.setattr(client, "_http", fake)
    return fake


@pytest.fixture
def cfg():
    c = AppConfig()
    c.ai = AIConfig(enabled=True)
    c.alerts = AlertsConfig(chat_id="42", intraday=False, filings=False, job_time="00:00")
    return c


@pytest.fixture(autouse=True)
def calm(monkeypatch):
    monkeypatch.setattr(guard, "_meminfo", lambda: {"ram_total_gb": 23.0, "ram_free_gb": 12.0})
    monkeypatch.setattr(guard, "_load1", lambda: 0.5)
    monkeypatch.setattr(guard, "_cpu_pct", lambda: 12.0)
    guard.record_tick(120)
    control._pull.update(running=False, model=None, pct=0.0, done=[], error=None, at=None)
    quality._run.update(running=False, done=0, n=0, at=None, error=None)
    q.clear("test setup")
    q._items.join()
    with get_db() as conn:
        conn.execute("INSERT INTO job_runs (job, run_date, started_at, finished_at, status, message) VALUES ('eod', ?, ?, ?, 'ok', '')",
                     (now_ist()[:10], now_ist(), now_ist()))


def weekdays_back(end: str, n: int) -> list[str]:
    d, out = date.fromisoformat(end), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def weekdays_after(start: str, n: int) -> list[str]:
    d, out = date.fromisoformat(start) + timedelta(days=1), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


_tok = iter(range(5000, 9000))


def bars(sym, closes, end=DAY, vols=None, days=None, token=None):
    """Daily bars ending on `end` (weekdays): high = close + 1, low = close − 1, volume 1000 (or `vols`)."""
    days = days or weekdays_back(end, len(closes))
    with get_db() as conn:
        r = conn.execute("SELECT instrument_token FROM instruments WHERE tradingsymbol = ?", (sym,)).fetchone()
        token = r[0] if r else (token or next(_tok))
        conn.execute("INSERT OR IGNORE INTO instruments (instrument_token, tradingsymbol, exchange, segment, fetched_on) VALUES (?, ?, 'NSE', 'NSE', ?)",
                     (token, sym, now_ist()))
        for i, (d, c) in enumerate(zip(days, closes)):
            v = vols[i] if vols else 1000
            conn.execute("INSERT OR REPLACE INTO candles_daily (instrument_token, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                         (token, d, c, c + 1, c - 1, c, v))
    return token


def trend(n=260, base=100.0):
    """An uptrend with swings: close = base + 0.25 i + 4 sin(i / 3)."""
    return [round(base + 0.25 * i + 4 * math.sin(i / 3), 2) for i in range(n)]


def item(sym, close, **kw):
    p = {"symbol": sym, "stage": "fresh", "pivot": round(close - 3, 2), "base_weeks": 6, "breakout_date": DAY, "volume_ratio": 1.8, "rs": 88,
         "score": 7.5, "close": close, "dist_to_pivot_pct": 2.1, **kw}
    return {"symbol": sym, "pivot": p["pivot"], "zone_top": round(p["pivot"] * 1.02, 2), "stop": round(close * 0.93, 2), "close": close, "payload": p}


def seed_plan(*syms):
    items = []
    for s in syms:
        c = trend()
        bars(s, c)
        items.append(item(s, c[-1]))
    pl.save_plan(DAY, items)
    return items


def view_row(sym, day, verdict="buy", score=72, source="plan", h20=None, h5=None, feats=None, shuffle=None, status="ok", fb=90.0):
    out = {}
    if h5 is not None:
        out["h5"] = {"pct": 1.0, "hit": h5}
    if h20 is not None:
        out["h20"] = {"pct": 4.0 if h20 else -2.0, "hit": h20}
    with get_db() as conn:
        cur = conn.execute("INSERT INTO ai_views (created, day, symbol, source, score, verdict, verdict_model, reasons, levels, features, status, outcome, "
                           "shuffle_score, prompt_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (now_ist(), day, sym, source, score, verdict, verdict, json.dumps(["tight base"]),
                            json.dumps({"support_1": fb, "support_2": None, "resistance": None, "fails_below": fb}),
                            json.dumps(feats or {"close": 100.0}), status, json.dumps(out) if out else None, shuffle, view.PROMPT_V))
    return cur.lastrowid


def beating_week(monday: str, k: int, beat=True):
    """One week of rows: 2 buys, 2 avoids and the plan baseline 50% (buy hits, avoid misses when `beat`)."""
    for i in range(2):
        view_row(f"B{k}{i}", monday, "buy", 75, h20=beat)
        view_row(f"A{k}{i}", monday, "avoid", 30, h20=not beat)
        view_row(f"P{k}{i}", monday, "wait", 50, source="plan", h20=bool(i))


# --- config and table ---------------------------------------------------------------------------------------------------------

def test_config_view_keys_and_defaults():
    a = AIConfig()
    assert (a.view_enabled, a.view_rate_held, a.view_rank_by_score, a.view_size_by_score, a.view_size_floor, a.view_exit_watch_alert,
            a.view_score_buy, a.view_score_avoid, a.view_max_per_night) == (True, True, False, False, 0.5, False, 60, 40, 40)
    assert set(view.VIEW_KEYS) <= set(vars(a)) and set(view.LOCKED) == {"view_rank_by_score", "view_size_by_score", "view_exit_watch_alert"}


def test_table_columns_and_unique_guard():
    with get_db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(ai_views)")]
    for c in ("id", "created", "day", "symbol", "source", "book", "rank_in_source", "score", "verdict", "verdict_model", "confidence", "reasons",
              "change", "would_change", "levels", "candidates", "features", "previous_id", "model", "prompt_version", "ms", "status", "outcome",
              "shuffle_score"):
        assert c in cols, c
    view_row("X", DAY)
    with pytest.raises(Exception):
        view_row("X", DAY)


# --- the facts ------------------------------------------------------------------------------------------------------------------

def test_pure_helpers_dry_up_atr_stack_round_step():
    bs = [{"date": str(i), "high": 101, "low": 99, "close": 100, "volume": 1000 if i < 40 else 500} for i in range(50)]
    assert view.dry_up(bs) == round((500 / 900 - 1) * 100, 1) and view.dry_up(bs[:20]) is None
    assert view.atr_pct(bs) == 2.0 and view.atr_pct(bs[:5]) is None
    assert view.sma_stack(120, 110, 100, 90) == "close > 20 > 50 > 200" and view.sma_stack(80, 110, 100, 90) == "under the 200-day"
    assert view.sma_stack(95, 110, 100, 90) == "50 > 200" and view.sma_stack(100, None, 1, 1) == "short history"
    assert [view.round_step(x) for x in (150, 500, 2500, 9000)] == [5, 10, 50, 100]


def test_features_from_the_plan_and_the_candles():
    c = trend()
    vols = [1000] * 250 + [400] * 10
    bars("AAA", c, vols=vols)
    with get_db() as conn:
        conn.execute("INSERT INTO corp_events (symbol, date, kind, purpose, fetched_at) VALUES ('AAA', '2026-11-20', 'results', 'Financial Results', ?)", (now_ist(),))
        conn.execute("INSERT INTO universe_snapshots (snapshot_date, index_name, symbol, industry) VALUES ('2026-11-01', 'N500', 'AAA', 'Cement')")
        conn.execute("INSERT INTO universe_snapshots (snapshot_date, index_name, symbol, industry) VALUES ('2026-11-01', 'N500', 'PEER', 'Cement')")
    bars("PEER", [100, 101, 102, 103, 104, 110])
    f = view.features("AAA", DAY, item("AAA", c[-1]))
    assert f["close"] == c[-1] and f["as_of"] == DAY and f["rs"] == 88 and f["volume_ratio"] == 1.8 and f["stage"] == "fresh" and f["stop"]
    assert f["sma_stack"] == "close > 20 > 50 > 200" or f["sma_stack"] in ("50 > 200", "mixed")
    assert f["sma50"] == round(sum(c[-50:]) / 50, 2) and f["dist_sma200_pct"] > 0 and f["atr_pct"] > 0
    assert f["dry_up_pct"] == round((400 / ((40 * 1000 + 10 * 400) / 50) - 1) * 100, 1) and f["vol_ratio5"] == round(400 / ((10 * 1000 + 10 * 400) / 20), 2)
    assert f["results_date"] == "2026-11-20" and f["results_in"] == 3
    assert f["industry"] == "Cement" and f["industry_move5_pct"] == 10.0 and f["chain"] == "no chain" and f["held"] is None


def test_features_held_facts_and_notes():
    c = trend()
    bars("HHH", c)
    with get_db() as conn:
        conn.execute("INSERT INTO ai_notes (created, day, kind, symbol, severity, title, text, data) VALUES (?, ?, 'filing', 'HHH', 'high', 'Pledge up', 't', '{}')",
                     (now_ist(), DAY))
    f = view.features("HHH", DAY, None, {"book": "real", "entry": 100.0, "stop": 150.0, "best": 210.0, "d": "2026-10-20"}, "held", 100.0)
    h = f["held"]
    assert h["entry"] == 100.0 and h["stop"] == 150.0 and h["days_held"] == 28 and h["trail_armed"] is True
    assert h["dist_to_stop_pct"] == round((c[-1] - 150) / c[-1] * 100, 2) and f["notes"] == [f"{DAY} filing (high): Pledge up"]


# --- the candidate levels and the pick --------------------------------------------------------------------------------------------

def test_levels_candidates():
    c = trend()
    bars("LLL", c)
    cands = view.levels("LLL", DAY, item("LLL", c[-1]))
    kinds = {x["kind"] for x in cands}
    assert 1 <= len(cands) <= view.MAX_CANDIDATES and [x["price"] for x in cands] == sorted((x["price"] for x in cands), reverse=True)
    assert {"pivot", "round", "vwap"} <= kinds and kinds & {"swing_low", "swing_high"}
    assert all(set(x) == {"price", "kind", "label"} for x in cands)
    lows, highs = view.swings([{"date": str(i), "low": v, "high": v + 2} for i, v in enumerate([5, 4, 3, 4, 5, 6, 5, 4, 3])])
    assert [x["low"] for x in lows] == [3] and [x["high"] for x in highs] == [8]


def test_snap_within_two_tenths_of_a_percent():
    cands = [{"price": 500.0, "kind": "pivot", "label": "pivot"}]
    assert view.snap(500.9, cands)["price"] == 500.0 and view.snap(501.2, cands) is None and view.snap("x", cands) is None


CANDS = [{"price": 520.0, "kind": "swing_high", "label": "swing high"}, {"price": 490.0, "kind": "pivot", "label": "pivot"},
         {"price": 470.0, "kind": "swing_low", "label": "swing low"}]
CTX = {"facts": "- close: 500.5\n- rs: 88\n- volume ratio: 1.8", "cands": CANDS, "close": 500.5, "held": False, "has_prev": False}


def answer(**kw):
    a = {"score": 72, "verdict": "buy", "confidence": 0.7, "reasons": ["RS 88 and volume ratio 1.8"], "change": "volume ratio 1.8 now",
         "levels": {"support_1": 490.4, "support_2": 470, "resistance": 520, "fails_below": 490}, "would_change": "a close under 490"}
    a.update(kw)
    return json.dumps(a)


def test_validate_ok_snaps_and_drops_change_without_previous():
    v = view.validate(answer(), CTX)
    assert v["score"] == 72 and v["verdict_model"] == "buy" and v["levels"] == {"support_1": 490.0, "support_2": 470.0, "resistance": 520.0, "fails_below": 490.0}
    assert v["change"] is None and view.validate(answer(), {**CTX, "has_prev": True})["change"] == "volume ratio 1.8 now"


@pytest.mark.parametrize("bad, fault", [
    (dict(verdict="hold"), "verdict must be one of"),
    (dict(score=140), "0-100"),
    (dict(reasons=["a" * 61]), "longer than 60"),
    (dict(reasons=["a", "b", "c", "d"]), "1-3 phrases"),
    (dict(confidence=3), "confidence"),
])
def test_validate_refusals(bad, fault):
    with pytest.raises(view.ViewInvalid, match=fault):
        view.validate(answer(**bad), CTX)


def test_validate_softens_figures_and_levels_instead_of_failing():
    """The three live faults of the first night: a figure not in the facts is stripped (numbers_checked False), a
    rounded level snaps to the nearest candidate within 1% (levels_checked False), a level the model invented falls
    back to the nearest candidate below the close; fails_below above the close falls back to support_1."""
    v = view.validate(answer(reasons=["RS 97 today", "volume ratio 1.8"]), CTX)
    assert v["numbers_checked"] is False and "97" not in v["reasons"][0] and v["reasons"][1] == "volume ratio 1.8" and v["score"] == 72
    v = view.validate(answer(levels={"support_1": 487.0, "support_2": 470, "resistance": 520, "fails_below": 487.0}), CTX)
    assert v["levels"]["support_1"] == 490.0 and v["levels"]["fails_below"] == 490.0 and v["levels_checked"] is False
    v = view.validate(answer(levels={"support_1": 123.45, "support_2": 3057.33, "resistance": 520, "fails_below": 9394.36}), CTX)
    assert v["levels"]["support_1"] == 490.0 and v["levels"]["support_2"] is None and v["levels"]["fails_below"] == 490.0 and v["levels_checked"] is False
    v = view.validate(answer(levels={"support_1": 490, "fails_below": 520}), CTX)
    assert v["levels"]["fails_below"] == 490.0 and v["levels"]["resistance"] is None
    v = view.validate(answer(levels={"support_1": 490}), CTX)
    assert v["levels"]["fails_below"] == 490.0
    ok = view.validate(answer(), CTX)
    assert ok["numbers_checked"] is True and ok["levels_checked"] is True
    with pytest.raises(view.ViewInvalid, match="no candidate below"):
        view.validate(answer(levels={"support_1": 1, "fails_below": 1}), {**CTX, "cands": [c for c in CANDS if c["price"] > 500.5]})


def test_validate_refuses_a_broker_name_and_non_json():
    from backend.ai import notes
    with pytest.raises(view.ViewInvalid, match="broker"):
        view.validate(answer(would_change=f"ask {notes.BLOCKED[0]}"), CTX)
    with pytest.raises(view.ViewInvalid, match="not JSON"):
        view.validate("nope", CTX)


# --- the verdict by code -----------------------------------------------------------------------------------------------------------

def test_verdict_recomputed_from_the_score(cfg):
    assert [view.verdict_for(s, False, cfg) for s in (60, 59, 40, 39)] == ["buy", "wait", "wait", "avoid"]
    assert [view.verdict_for(s, True, cfg) for s in (80, 50, 30)] == ["hold", "trim", "exit-watch"]
    cfg.ai.view_score_buy = 70
    assert view.verdict_for(65, False, cfg) == "wait"


def test_held_exit_watch_on_a_broken_fails_below(cfg):
    assert view.verdict_for(85, True, cfg, close=480.0, fails_below=490.0) == "exit-watch"
    assert view.verdict_for(85, True, cfg, close=495.0, fails_below=490.0) == "hold"


def test_rate_keeps_the_models_verdict_apart(cfg, ollama, model):
    seed_plan("AAA")
    model.score, model.verdict = 82, "avoid"
    v = view.rate(cfg, {"symbol": "AAA", "source": "plan", "item": pl.latest_plan()[1][0], "rank": 1}, DAY)
    assert v["status"] == "ok" and v["score"] == 82 and v["verdict"] == "buy" and v["verdict_model"] == "avoid"
    assert v["levels"]["fails_below"] <= v["features"]["close"] and v["candidates"] and v["prompt_version"] == "view-v1" and v["rank_in_source"] == 1


def test_rate_one_retry_naming_the_fault_then_failed(cfg, ollama, model):
    seed_plan("AAA", "BBB")
    its = pl.latest_plan()[1]
    model.bad = ["not json"]
    v = view.rate(cfg, {"symbol": "AAA", "source": "plan", "item": its[0]}, DAY)
    gens = [b for _, p, b in ollama.calls if p == "/api/generate"]
    assert v["status"] == "ok" and len(gens) == 2 and "Your previous answer was not valid: not JSON" in gens[1]["system"]
    model.bad = "still not json"
    v = view.rate(cfg, {"symbol": "BBB", "source": "plan", "item": its[1]}, DAY)
    assert v["status"] == "failed" and v["score"] is None and v["verdict"] is None and "error" in v["features"]
    assert view.record()["failed"] == 1


# --- memory -------------------------------------------------------------------------------------------------------------------------

def test_previous_view_block_after_the_facts_and_the_change(cfg, ollama, model):
    c = trend()
    bars("AAA", c)
    it = {"symbol": "AAA", "source": "plan", "item": item("AAA", c[-2])}
    with get_db() as conn:
        conn.execute("DELETE FROM candles_daily WHERE date = ?", (DAY,))
    first = view.rate(cfg, it, "2026-11-16")
    bars("AAA", c)
    model.change, model.score = "volume dried up since the last view", 68
    second = view.rate(cfg, {**it, "item": item("AAA", c[-1])}, DAY)
    prompt = [b for _, p, b in ollama.calls if p == "/api/generate"][-1]["prompt"]
    assert prompt.index("Facts:") < prompt.index("Previous view (context, not an anchor):") and "- 2026-11-16: score 72 · buy" in prompt
    assert "- since then: close " in prompt and "support 1" in prompt
    assert second["previous_id"] == first["id"] and second["change"] == "volume dried up since the last view"
    assert view.latest(["AAA"])["AAA"]["previous"] == {"id": first["id"], "day": "2026-11-16", "score": 72, "verdict": "buy", "reasons": first["reasons"]}


def test_calibration_note_from_outcomes(cfg, ollama):
    assert view.build_calibration() is None
    view_row("A1", "2026-10-01", "buy", h20=True, feats={"close": 100, "results_in": 2})
    view_row("A2", "2026-10-01", "buy", h20=False, feats={"close": 100, "results_in": 1})
    view_row("A3", "2026-10-01", "buy", h20=False, feats={"close": 100, "results_in": 3})
    view_row("A4", "2026-10-01", "avoid", h20=False)
    note = view.build_calibration()
    assert "your buy verdicts hit 33.3% at 20 sessions (3 votes)" in note and "your avoid verdicts hit 0.0%" in note
    assert "your buy verdicts in results weeks hit 33.3%" in note
    seed_plan("AAA")
    with get_db() as conn:
        from backend.database.models import set_setting
        set_setting(conn, view.CALIB_KEY, note)
    view.rate(cfg, {"symbol": "AAA", "source": "plan", "item": pl.latest_plan()[1][0]}, DAY)
    assert "Calibration (your record so far): your buy verdicts hit 33.3%" in [b for _, p, b in ollama.calls if p == "/api/generate"][-1]["prompt"]


# --- the night: order, cap, outcomes, shuffle -----------------------------------------------------------------------------------------

def test_scope_order_plan_held_follow_screens(cfg, monkeypatch):
    monkeypatch.setattr(view, "_plan_items", lambda: [{"symbol": "P1", "source": "plan", "book": None}, {"symbol": "P2", "source": "plan", "book": "real"}])
    monkeypatch.setattr(view, "_held_items", lambda: [{"symbol": "H1", "source": "held", "book": "real", "held": {}}, {"symbol": "P1", "source": "held", "book": "paper"}])
    monkeypatch.setattr(view, "_follow_items", lambda d: [{"symbol": "W1", "source": "follow", "book": None}])
    monkeypatch.setattr(view, "_screen_items", lambda d: [{"symbol": "S1", "source": "screen:vcp", "book": None}])
    s = view.scope(cfg, DAY)
    assert [(x["symbol"], x["source"], x["rank"]) for x in s] == [("P1", "plan", 1), ("P2", "plan", 2), ("H1", "held", 1),
                                                                  ("W1", "follow", 1), ("S1", "screen:vcp", 1)]
    cfg.ai.view_rate_held = False
    assert "H1" not in [x["symbol"] for x in view.scope(cfg, DAY)]


def test_follow_and_screen_items_from_the_scan(cfg):
    with get_db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(scan_runs)")]
        vals = {c: (DAY if c == "as_of" else now_ist() if "at" in c else 0) for c in cols if c != "id"}
        conn.execute(f"INSERT OR IGNORE INTO scan_runs ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})", tuple(vals.values()))
        for scr, sym, stage, bd, score in (("vcp", "F1", "fresh", DAY, 5), ("vcp", "F2", "fresh", "2026-11-10", 9), ("vcp", "R1", "forming", None, 3),
                                           ("flag", "R2", "forming", None, 8)):
            conn.execute("INSERT INTO scan_results (as_of, screen, symbol, stage, payload) VALUES (?,?,?,?,?)",
                         (DAY, scr, sym, stage, json.dumps({"symbol": sym, "breakout_date": bd, "score": score})))
    assert [x["symbol"] for x in view._follow_items(DAY)] == ["F1"]
    sc = view._screen_items(DAY)
    assert [(x["symbol"], x["source"], x["item"]["stage"]) for x in sc] == [("R2", "screen:flag", "ready"), ("F2", "screen:vcp", "breakout"),
                                                                            ("F1", "screen:vcp", "breakout"), ("R1", "screen:vcp", "ready")]


def test_run_night_rates_in_order_within_the_cap(cfg, ollama):
    seed_plan("AAA", "BBB", "CCC")
    cfg.ai.view_max_per_night = 2
    fake = FakeTG()
    out = view.run_night(cfg, NIGHT, Notifier(cfg.alerts, fake), rng=random.Random(1))
    rs = view.rows()
    assert [r["symbol"] for r in rs] == ["AAA", "BBB"] and "2 rated" in out["note"] and "1 over the cap" in out["note"]
    ln = view.last_night()
    assert ln["n"] == 2 and ln["by_verdict"] == {"buy": 2} and ln["capped"] == 1 and ln["day"] == DAY
    assert any("🌙 <b>AI view · tonight</b> · 2 rated" in t for _, t in fake.sent)
    view.run_night(cfg, NIGHT, Notifier(cfg.alerts, FakeTG()), rng=random.Random(1))       # the next run carries on, never re-rates
    assert [r["symbol"] for r in view.rows()] == ["AAA", "BBB", "CCC"]


def test_outcome_for_hit_rules():
    v = {"source": "plan", "features": {"close": 100.0, "stop": 95.0}, "levels": {"fails_below": 96.0}, "outcome": None}
    up = [{"close": 100 + i * 0.5} for i in range(1, 21)]
    o = view.outcome_for(v, up)
    assert o["h5"] == {"pct": 2.5, "hit": False} and o["h10"] == {"pct": 5.0, "hit": True} and o["h20"]["hit"] is True
    stop_first = [{"close": 94.0}] + [{"close": 120.0}] * 19
    assert view.outcome_for(v, stop_first, late_at=10.0)["h5"]["hit"] is True               # +20% at the horizon
    trail = [{"close": 111.0}] + [{"close": 99.0}] * 19
    assert view.outcome_for(v, trail, late_at=10.0)["h5"] == {"pct": -1.0, "hit": True}     # the trail armed first
    assert view.outcome_for(v, [{"close": 94.0}, {"close": 111.0}] + [{"close": 99.0}] * 3, late_at=10.0)["h5"]["hit"] is False
    held = {"source": "held", "features": {"close": 100.0}, "levels": {"fails_below": 96.0}, "outcome": None}
    assert view.outcome_for(held, [{"close": 97.0}] * 5)["h5"]["hit"] is True and view.outcome_for(held, [{"close": 95.0}] * 5)["h5"]["hit"] is False
    assert view.outcome_for(v, up[:4]) == {}


def test_score_outcomes_at_5_10_20(cfg):
    c = trend(60)
    days = weekdays_back("2026-10-01", 60)
    bars("OUT", c, days=days)
    vid = view_row("OUT", "2026-10-01", feats={"close": c[-1], "stop": 1.0})
    after = weekdays_after("2026-10-01", 12)
    bars("OUT", [c[-1] * 1.05] * 12, days=after)
    assert view.score_outcomes(cfg) == 1
    o = view.get(vid)["outcome"]
    assert set(o) == {"h5", "h10", "scored_at"} and o["h5"] == {"pct": 5.0, "hit": True}
    bars("OUT", [c[-1] * 0.99] * 10, days=weekdays_after(after[-1], 10))
    assert view.score_outcomes(cfg) == 1 and view.get(vid)["outcome"]["h20"] == {"pct": -1.0, "hit": False}
    assert view.score_outcomes(cfg) == 0


def test_shuffle_test_about_one_in_five(cfg, ollama, model):
    seed_plan(*[f"S{i:02d}" for i in range(10)])
    rated = [view.rate(cfg, {"symbol": i["symbol"], "source": "plan", "item": i}, DAY) for i in pl.latest_plan()[1]]

    class Rng:
        _n = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0]

        def random(self):
            return 0.1 if self._n.pop(0) else 0.9

        def choice(self, xs):
            return xs[0]
    rng = Rng()
    model.score = 31
    assert view.shuffle_test(cfg, rated, rng) == 2
    sh = [r for r in view.rows() if r["shuffle_score"] is not None]
    assert [r["symbol"] for r in sh] == ["S00", "S05"] and all(r["shuffle_score"] == 31 and r["score"] == 72 for r in sh)
    assert view.shuffle_test(cfg, rated[:1]) == 0                                            # needs another stock to swap in


# --- the record and the unlock rule --------------------------------------------------------------------------------------------------

def test_record_shape_by_verdict_baseline_and_week():
    view_row("B1", "2026-10-05", "buy", h20=True, h5=True, feats={"close": 100, "dry_up_pct": -40, "rs": 90, "volume_ratio": 2.0})
    view_row("B2", "2026-10-05", "buy", h20=False, feats={"close": 100, "dry_up_pct": -30, "rs": 85, "volume_ratio": 1.2})
    view_row("V1", "2026-10-05", "avoid", 30, h20=False, feats={"close": 100, "dry_up_pct": 20, "rs": 60, "volume_ratio": 1.1})
    view_row("F1", "2026-10-05", status="failed")
    r = view.record()
    assert r["by_verdict"]["buy"] == {"votes": 2, "hit5": 100.0, "hit20": 50.0, "avg20": 1.0} and r["by_verdict"]["avoid"]["hit20"] == 0.0
    assert r["baseline"]["votes"] == 3 and r["baseline"]["hit20"] == 33.3 and r["failed"] == 1 and r["total"] == 4
    assert r["by_week"] == [{"week": "2026-W41", "from": "2026-10-05", "votes": 3, "buy_hit20": 50.0, "avoid_hit20": 0.0, "baseline20": 33.3, "beat": True}]
    assert r["leans"]["feature"] == "dry_up_pct" and r["leans"]["by_verdict"]["buy"]["rs"] == 87.5
    assert set(r) >= {"by_verdict", "baseline", "by_week", "shuffle", "leans", "worst", "unlocked", "unlock", "failed", "total"}
    s = view.record_summary(r)
    assert s == {"buy_hit20": 50.0, "avoid_hit20": 0.0, "baseline20": 33.3, "edge": 16.7, "unlocked": False, "shuffle_ok": None}


def test_record_worst_bucket_and_shuffle():
    for i, hit in enumerate((False, False, True)):
        view_row(f"R{i}", "2026-10-05", "buy", h20=hit, feats={"close": 100, "results_in": 2, "volume_ratio": 2.0}, shuffle=30)
    for i in range(3):
        view_row(f"N{i}", "2026-10-05", "buy", h20=True, feats={"close": 100, "volume_ratio": 2.0}, shuffle=80)
    r = view.record()
    assert r["worst"]["bucket"] == "buys in results weeks" and r["worst"]["hit20"] == 33.3 and "hit 33.3% at 20 sessions (3 votes)" in r["worst"]["text"]
    assert r["shuffle"]["n"] == 6 and r["shuffle"]["real_hit20"] == 66.7 and r["shuffle"]["shuffled_hit20"] == 100.0 and r["shuffle"]["ok"] is False


def test_unlock_rule_four_weeks_in_a_row():
    mondays = ["2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26", "2026-11-02"]
    beating_week(mondays[0], 0, beat=False)
    for k, m in enumerate(mondays[1:4], start=1):
        beating_week(m, k)
    r = view.record()
    assert r["unlock"]["streak"] == 3 and r["unlocked"] is False and view.unlocked() is False and "3 of 4" in r["unlock"]["text"]
    beating_week(mondays[4], 4)
    assert view.unlocked() is True and view.record()["unlock"]["streak"] == 4


# --- the switches: the lock and the bounds ------------------------------------------------------------------------------------------------

def test_apply_settings_lock_and_bounds(cfg, monkeypatch):
    for k in view.LOCKED:
        with pytest.raises(ValueError, match="locked"):
            view.apply_settings(cfg, {k: True})
    assert view.apply_settings(cfg, {"view_score_buy": 65, "enabled": True}) == {"enabled": True} and cfg.ai.view_score_buy == 65
    with pytest.raises(ValueError, match="0.5-1.0"):
        view.apply_settings(cfg, {"view_size_floor": 0.3})
    with pytest.raises(ValueError, match="under view_score_buy"):
        view.apply_settings(cfg, {"view_score_avoid": 70})
    view.apply_settings(cfg, {"view_rank_by_score": False})                                 # turning off is always fine
    monkeypatch.setattr(view, "unlocked", lambda: True)
    view.apply_settings(cfg, {"view_size_by_score": True})
    assert cfg.ai.view_size_by_score is True


# --- the locked switches' wiring --------------------------------------------------------------------------------------------------------

def test_size_by_score_math_floor_and_never_above_one():
    assert [view.size_mult(s) for s in (100, 80, 72, 60, 40, 0)] == [1.0, 1.0, 0.9, 0.75, 0.5, 0.5]
    assert view.size_mult(40, 0.8) == 0.8 and view.size_mult(None) == 1.0
    assert all(view.size_mult(s, f) <= 1.0 for s in range(0, 101, 5) for f in (0.5, 0.7, 1.0))
    assert view.ai_qty(121, 64) == 96 and view.ai_qty(121, 99) == 121 and view.ai_qty(1, 0) == 1 and view.ai_qty(3, 0) == 1
    assert all(1 <= view.ai_qty(q_, s) <= q_ for q_ in (1, 2, 7, 121) for s in range(0, 101, 7))


def real_state(buys=("PLAN1", "PLAN2"), held=()):
    orders = [{"action": "BUY", "symbol": s, "ts": s, "qty": 40, "stop": 470.0, "close": 500.0, "why": "breakout"} for s in buys]
    st = {"started": "2026-10-01", "capital": 300_000.0, "cash": 200_000.0, "charges": 0.0, "as_of": DAY, "orders": orders, "decided": DAY,
          "positions": {s: {"qty": 16, "px": 1100.0, "last": 1150.0, "d": "2026-10-20", "stop": 1000.0, "charges": 0.0, "ts": s, "days": 15} for s in held},
          "cap": 10, "allowed": True, "waiting": 0, "filter": {"name": "E+", "state": "FULL", "slots": 10}}
    real._save(st)
    return st


def test_send_orders_size_by_score_never_above_never_a_skip_never_a_stop_change(cfg):
    real_state(("PLAN1", "PLAN2", "PLAN3"))
    view_row("PLAN1", DAY, "buy", 64)
    view_row("PLAN2", DAY, "avoid", 10)
    cfg.real.enabled = True
    today = "2026-11-18"
    sent = []

    def sender(ts, action, qty, stop=None):
        sent.append((ts, qty, stop))
        return "ok"
    cfg.ai.view_size_by_score = False
    m = real.morning(cfg, datetime(2026, 11, 18, 9, 0, tzinfo=IST))
    b1 = next(o for o in m["orders"] if o["symbol"] == "PLAN1")
    assert b1["qty_ai"] == 40 and b1["ai_view"]["score"] == 64 and m["size_by_score"] is False
    cfg.ai.view_size_by_score = True
    m = real.morning(cfg, datetime(2026, 11, 18, 9, 0, tzinfo=IST))
    q1, q2, q3 = (next(o for o in m["orders"] if o["symbol"] == s) for s in ("PLAN1", "PLAN2", "PLAN3"))
    assert (q1["qty_ai"], q1["size_x"], q2["qty_ai"], q2["size_x"], q3["qty_ai"]) == (32, 0.8, 20, 0.5, 40)
    real.full_size(cfg, "PLAN2", datetime(2026, 11, 18, 9, 1, tzinfo=IST))
    res = real.send_orders(cfg, None, today, sender)
    assert sorted((s[0], s[1]) for s in sent) == [("PLAN1", 32), ("PLAN2", 40), ("PLAN3", 40)]
    assert len(res["sent"]) == 3 and not res["skipped"] and all(o["stop"] == 470.0 for o in res["sent"])
    assert real._state()["positions"]["PLAN1"]["qty"] == 32 and real._state()["positions"]["PLAN1"]["stop"] == 470.0
    with pytest.raises(ValueError, match="already"):
        real.full_size(cfg, "PLAN1", datetime(2026, 11, 18, 9, 30, tzinfo=IST))


def test_telegram_full_symbol_and_the_dry_run_line(cfg):
    from backend.alerts.commands import BotCommands
    real_state(("PLAN1",))
    view_row("PLAN1", DAY, "buy", 64)
    cfg.real.enabled = True
    cfg.ai.view_size_by_score = True
    when = datetime(2026, 11, 18, 9, 2, tzinfo=IST)
    st = LoginState()
    st.config = cfg
    fake = BotTG([update(1, "full PLAN1", at=when), update(2, "full", at=when)])
    assert BotCommands(st, clock=lambda: when).poll_once(Notifier(cfg.alerts, fake)) == 2
    a, b = (h for _, h in fake.sent)
    assert a.startswith("↩️ PLAN1 goes out at the rule size today.") and "🟢 BUY <b>PLAN1</b> 40 ·" in a and "🤖 AI 64 buy · <i>AI ·</i> <i>tight base</i>" in a
    assert b == "Which stock? /full SYMBOL" and real._state()["ai_full"]["2026-11-18"] == ["PLAN1"]
    real._save({**real._state(), "ai_full": {}})
    m = real.morning(cfg, when)
    text = msg.real_preview([o for o in real._state()["orders"]], "E", "09:16", True, False, m)
    assert "🟢 BUY <b>PLAN1</b> 32 (0.8× · was 40) · stop" in text and "9:16 AM" in text and no_brokers(text)


def test_rank_by_score_only_reorders_ties(cfg):
    items = [("A", 5.0), ("B", 3.0), ("C", 3.0), ("D", 3.0), ("E", 1.0)]
    scores = {"B": 40, "C": 90, "D": None, "E": 99, "A": 10}
    out = view.order_ties(items, lambda x: x[1], lambda x: x[0], scores)
    assert [x[0] for x in out] == ["A", "C", "B", "D", "E"]
    assert view.rank_ties(cfg, items, lambda x: x[1], lambda x: x[0], DAY) == items          # off: unchanged
    view_row("C", "2026-11-16", score=90)
    view_row("B", "2026-11-16", score=40)
    cfg.ai.view_rank_by_score = True
    assert [x[0] for x in view.rank_ties(cfg, items, lambda x: x[1], lambda x: x[0], DAY)] == ["A", "C", "B", "D", "E"]


def test_exit_watch_alert_only_when_on(cfg):
    real_state((), held=("HELD1",))
    bars("HELD1", [500.0, 480.0], days=["2026-11-16", DAY])
    view_row("HELD1", "2026-11-16", "hold", 70, source="held", fb=490.0)
    fake = FakeTG()
    assert view.exit_watch(cfg, DAY, Notifier(cfg.alerts, fake)) == [] and fake.sent == []
    cfg.ai.view_exit_watch_alert = True
    out = view.exit_watch(cfg, DAY, Notifier(cfg.alerts, fake))
    assert [x["symbol"] for x in out] == ["HELD1"] and len(fake.sent) == 1
    t = fake.sent[0][1]
    assert t.startswith("🤖 <b>HELD1</b> · exit-watch · closed ₹480.00 under ₹490.00") and "nothing is sold and the stop stays" in t
    assert real._state()["positions"]["HELD1"]["stop"] == 1000.0


# --- the joins and the API ----------------------------------------------------------------------------------------------------------------

def test_attach_prefers_the_matching_source_and_skips_old_views():
    view_row("AAA", DAY, "wait", 55, source="screen:vcp")
    view_row("AAA", DAY, "buy", 70, source="plan")
    view_row("OLD", "2026-10-01", "buy", 70)
    rows = view.attach([{"symbol": "AAA"}, {"symbol": "OLD"}, {"symbol": "NONE"}, "junk"], "screen:vcp", DAY)
    assert rows[0]["ai_view"]["score"] == 55 and rows[0]["ai_view"]["source"] == "screen:vcp" and rows[1]["ai_view"] is None and rows[2]["ai_view"] is None
    assert set(rows[0]["ai_view"]) == {"score", "verdict", "reasons", "change", "levels", "at", "day", "source", "prev_score"}
    assert view.attach([{"symbol": "AAA"}], "follow", DAY)[0]["ai_view"]["source"] in ("plan", "screen:vcp")


def test_run_eod_queues_the_view_after_the_suggestions(monkeypatch, cfg):
    from backend.ai import notes, suggest, why
    from backend.alerts.jobs import Scheduler
    st = LoginState()
    st.config = cfg
    sch = Scheduler(st, notifier_factory=lambda c: Notifier(c, FakeTG()))
    calls = []
    monkeypatch.setattr(sch, "_run_eod", lambda now, force: {"status": "ok"})
    monkeypatch.setattr(notes, "enqueue_tonight", lambda c, now, notifier=None: calls.append("notes") or {})
    monkeypatch.setattr(why, "enqueue_tonight", lambda c, now, notifier=None: calls.append("why") or {})
    monkeypatch.setattr(suggest, "enqueue_tonight", lambda c, now, notifier=None: calls.append("suggest") or {})
    monkeypatch.setattr(view, "enqueue_tonight", lambda c, now, notifier=None: calls.append("view") or {"id": 9})
    assert sch.run_eod(NIGHT)["ai_view"] == {"id": 9} and calls == ["notes", "why", "suggest", "view"]
    monkeypatch.setattr(view, "enqueue_tonight", lambda c, now, notifier=None: (_ for _ in ()).throw(RuntimeError("x")))
    assert sch.run_eod(NIGHT)["ai_view"]["status"] == "failed"                               # never breaks the evening job


def test_enqueue_off_and_the_queue_job(cfg, ollama):
    cfg.ai.view_enabled = False
    assert view.enqueue_tonight(cfg, NIGHT) == {"status": "off"}
    cfg.ai.view_enabled = True
    seed_plan("AAA")
    job = view.enqueue_tonight(cfg, NIGHT, Notifier(cfg.alerts, FakeTG()))
    assert job["key"] == f"view:{DAY}"
    row = q.wait(job["id"], 30)
    assert row["status"] == "done" and "1 rated" in row["note"] and view.running() is False


def test_messages_night_week_and_no_broker_names():
    night = {"n": 7, "by_verdict": {"buy": 3, "wait": 2, "hold": 2}, "failed": 1, "capped": 0, "at": "2026-11-17T21:42:00+05:30"}
    t = msg.ai_view_night(night)
    assert t.startswith("🌙 <b>AI view · tonight</b> · 7 rated · 1 failed\n🟢 3 buy · 🟡 2 wait · 🟢 2 hold") and "9:42 PM" in t
    w = {"week": "2026-W46", "buy_hit20": 58.0, "avoid_hit20": 31.0, "baseline20": 44.0, "beat": True}
    a = msg.ai_view_week(w, {"unlock": {"unlocked": False, "streak": 2, "need": 4}})
    assert a.startswith("📊 <b>AI view · week 46</b> · buy hit@20 58% · avoid 31% · plan 44% · beat the plan") and "🔒 locked · 2 of 4" in a
    assert "🔓 <b>unlocked</b>" in msg.ai_view_week(w, {"unlock": {"unlocked": True}})
    assert msg.ai_view_line(None) == "" and all(no_brokers(x) for x in (t, a))
    assert no_brokers(view.SYSTEM_PROMPT) and "never" in view.SYSTEM_PROMPT


def test_weekly_record_line_once_a_week(cfg, ollama):
    beating_week("2026-11-09", 1)
    seed_plan("AAA")
    fake = FakeTG()
    view.run_night(cfg, NIGHT, Notifier(cfg.alerts, fake), rng=random.Random(2))
    weeks = [t for _, t in fake.sent if t.startswith("📊")]
    assert len(weeks) == 1 and "week 46" in weeks[0]
    fake2 = FakeTG()
    view.run_night(cfg, NIGHT, Notifier(cfg.alerts, fake2), rng=random.Random(2))
    assert not [t for _, t in fake2.sent if t.startswith("📊")]


def test_ask_levels_tool(cfg):
    from backend.ai import ask
    c = trend()
    bars("LVL", c)
    view_row("LVL", DAY, "buy", 70, fb=480.0)
    out = ask.TOOLS["levels"]["fn"](cfg, "lvl")
    assert "levels" in ask.TOOL_NAMES and out["symbol"] == "LVL" and out["candidates"] and out["picked"]["fails_below"] == 480.0
    assert out["view"]["score"] == 70 and ask.coerce_args("levels", {"symbol": "LVL!"}) == {"symbol": "LVL"}


@pytest.fixture
def api(fake_kite, make_client, monkeypatch, ollama):
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("KITE_API_KEY", "key123")
    monkeypatch.setenv("KITE_API_SECRET", "sec")
    from fastapi.testclient import TestClient
    from backend import main
    kc = make_client(fake_kite, history_years=1)
    kc.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc_, ic: kc)
    with TestClient(main.app) as tc:
        yield tc


def test_routes_view_record_run_and_settings(api):
    tc = api
    today = now_ist()[:10]
    p = view_row("AAA", today, "wait", 44)
    with get_db() as conn:
        vid = view_row("AAA", (date.fromisoformat(today) + timedelta(days=1)).isoformat(), "buy", 68)
        conn.execute("UPDATE ai_views SET previous_id = ?, change = 'volume dried up' WHERE id = ?", (p, vid))
    v = tc.get("/api/ai/view?symbols=AAA,zzz").json()["views"]
    assert list(v) == ["AAA"] and v["AAA"]["score"] == 68 and v["AAA"]["previous"]["score"] == 44 and v["AAA"]["change"] == "volume dried up"
    assert set(v["AAA"]) >= {"levels", "candidates", "features", "reasons", "verdict_model", "outcome", "shuffle_score", "previous"}
    r = tc.get("/api/ai/view/record?weeks=8").json()
    assert set(r) >= {"by_verdict", "baseline", "by_week", "shuffle", "leans", "worst", "unlocked", "unlock"} and r["weeks"] == 8
    ai = tc.get("/api/ai").json()["view"]
    assert ai["on"] is True and set(ai) == {"on", "last_night", "record_summary", "switches"}
    assert set(ai["record_summary"]) == {"buy_hit20", "avoid_hit20", "baseline20", "edge", "unlocked", "shuffle_ok"}
    assert ai["switches"]["rank_by_score"] is False and ai["switches"]["locked"] is True and ai["switches"]["score_buy"] == 60
    assert tc.put("/api/ai/settings", json={"view_size_by_score": True}).status_code == 400
    assert tc.put("/api/ai/settings", json={"view_exit_watch_alert": True}).status_code == 400
    assert tc.put("/api/config/ai", json={"view_rank_by_score": True}).status_code == 400
    assert tc.put("/api/config/ai", json={"view_size_floor": 1.4}).status_code == 400
    assert tc.put("/api/ai/settings", json={"view_max_per_night": 25}).status_code == 200 and tc.get("/api/config/ai").json()["view_max_per_night"] == 25
    assert tc.put("/api/config/ai", json={"view_score_buy": 65}).status_code == 200
    first = tc.post("/api/ai/view/run")
    assert first.status_code == 200


def test_routes_ai_view_joins(api):
    tc = api
    today = now_ist()[:10]
    pl.save_plan(today, [{"symbol": "AAA", "pivot": 10.0, "zone_top": 10.5, "stop": 9.0}, {"symbol": "BBB", "pivot": 10.0, "zone_top": 10.5, "stop": 9.0}])
    view_row("AAA", today, "buy", 70)
    view_row("HELD1", today, "trim", 50, source="held")
    items = tc.get("/api/plan").json()["items"]
    assert items[0]["ai_view"]["score"] == 70 and items[0]["ai_view"]["verdict"] == "buy" and items[1]["ai_view"] is None
    real_state(("AAA",), held=("HELD1",))
    pos = tc.get("/api/real").json()["positions"]
    assert pos[0]["symbol"] == "HELD1" and pos[0]["ai_view"]["verdict"] == "trim"
    assert tc.get("/api/breakout").status_code == 200
    with get_db() as conn:
        from backend.database.models import set_setting
        set_setting(conn, "breakout.scan", json.dumps({"as_of": today, "buys": [{"symbol": "AAA", "close": 10.0}]}))
    assert tc.get("/api/breakout").json()["scan"]["buys"][0]["ai_view"]["score"] == 70
    f = tc.get("/api/follow").json()
    assert all("ai_view" in r for r in f.get("rows") or [])
