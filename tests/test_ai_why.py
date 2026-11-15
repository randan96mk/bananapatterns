"""🤖 Local AI, PR 3 · B (trade "why" notes): the closed trades of the four books normalised, the held names that gapped
or hit a stop, the evidence lines (the trade row, the day move, the industry's average, NIFTY, filings and deals), the
one-line validation (the "likely cause" prefix, numbers only from the evidence, no broker), the night job through the
fake Ollama (rows of kind "why", the UNIQUE guard, the evening message once), the `ai_why` on the trade tables, the
hook and the route. Fake Ollama; no network."""

import json
from datetime import datetime

import pytest

from backend.ai import client, control, guard, notes, quality, why
from backend.ai import queue as q
from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier
from backend.config import IST, AIConfig, AlertsConfig, AppConfig
from backend.database.models import get_db, now_ist
from backend.engine import real
from backend.options import instruments as oi
from tests.test_ai_infra import no_brokers
from tests.test_ai_notes import FakeNotes, hold
from tests.test_alerts import FakeTG

NIGHT = datetime(2026, 11, 17, 21, 0, tzinfo=IST)
DAY, PREV = "2026-11-17", "2026-11-16"
LINE = json.dumps({"why": "likely cause: the stock gapped -2.10% with its industry (-1.40%) after the results filing; the stop at 940.00 gave way."})


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeNotes(reply=LINE)
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


def trade(table, payload, **cols):
    with get_db() as conn:
        keys = ", ".join(["payload", *cols])
        cur = conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({','.join('?' * (len(cols) + 1))})", (json.dumps(payload), *cols.values()))
    return cur.lastrowid


def bars(sym, prev_close, open_, close, token=None, industry=None):
    """Two daily bars (PREV, DAY) for `sym`, its instrument row and, if given, its industry in the universe table."""
    token = token or (abs(hash(sym)) % 100000 + 1000)
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO instruments (instrument_token, tradingsymbol, exchange, segment, fetched_on) VALUES (?, ?, 'NSE', 'NSE', ?)",
                     (token, sym, now_ist()))
        conn.execute("INSERT OR REPLACE INTO candles_daily VALUES (?,?,?,?,?,?,1000)", (token, PREV, prev_close, prev_close, prev_close, prev_close))
        conn.execute("INSERT OR REPLACE INTO candles_daily VALUES (?,?,?,?,?,?,1000)", (token, DAY, open_, max(open_, close), min(open_, close), close))
        if industry:
            conn.execute("INSERT OR REPLACE INTO universe_snapshots (snapshot_date, index_name, symbol, industry) VALUES ('2026-11-01', 'NIFTY 500', ?, ?)",
                         (sym, industry))
    return token


def seed_books():
    """One trade closed today in each book (and one real trade closed earlier), the bars and a filing."""
    ids = {"real": trade("real_trades", {"symbol": "RT", "qty": 10, "in": "2026-11-03", "out": DAY, "buy": 1000.0, "sell": 940.0, "stop": 940.0, "days": 10,
                                         "gross": -600.0, "costs": 20.0, "net": -620.0, "why": "stop hit", "best": 1050.0}),
           "old": trade("real_trades", {"symbol": "OLD", "qty": 5, "in": "2026-10-03", "out": PREV, "buy": 100.0, "sell": 110.0, "stop": 95.0, "days": 30,
                                        "gross": 50.0, "costs": 2.0, "net": 48.0, "why": "exit"}),
           "paper": trade("paper_trades", {"symbol": "PT", "qty": 20, "in": "2026-11-10", "out": DAY, "buy": 500.0, "sell": 520.0, "stop": 470.0, "days": 5,
                                           "gross": 400.0, "costs": 10.0, "net": 390.0, "why": "close under SMA50"}, account="E"),
           "futures": trade("futures_trades", {"symbol": "FT", "side": "short", "book": "paper", "lots": 1, "lot_size": 500, "qty": 500, "in": "2026-11-12", "out": DAY,
                                               "entry": 200.0, "exit": 206.0, "stop": 206.0, "days": 3, "net": -3100.0, "why": "stop hit", "contract": "FT26NOVFUT", "rolls": 0},
                            account="paper_short"),
           "options": trade("options_trades", {"day": DAY, "n": 1, "bias": "bear", "lots": 1, "qty": 75, "sold": {"strike": 25500, "type": "CE", "entry": 80.0, "exit": 112.0},
                                               "hedge": {"strike": 25750, "type": "CE"}, "credit": 60.0, "cost": 92.0, "net": -2450.0, "why": "stop", "label": "premium stop",
                                               "in": "10:05 AM", "out": "11:20 AM", "score": 2, "spot_in": 25400, "spot_out": 25620, "chain_line": "PCR 0.8", "be_t": None}, day=DAY)}
    bars("RT", 1000.0, 979.0, 950.0, industry="Chemicals")
    bars("PEER", 100.0, 99.0, 98.6, industry="Chemicals")
    bars("PT", 500.0, 505.0, 520.0)
    bars("FT", 200.0, 203.0, 206.0)
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO candles_daily VALUES (?,?,?,?,?,?,0)", (oi.INDEX_TOKEN, PREV, 25000, 25000, 25000, 25000))
        conn.execute("INSERT OR REPLACE INTO candles_daily VALUES (?,?,?,?,?,?,0)", (oi.INDEX_TOKEN, DAY, 25100, 25700, 25000, 25600))
        conn.execute("INSERT INTO filings (seq_id, symbol, dt, category, text, url, kind, fetched_at) VALUES ('77', 'RT', ?, 'Outcome of Board Meeting', "
                     "'Net loss of Rs. 12.4 crore for the quarter', 'https://nsearchives.nseindia.com/RT.pdf', 'results', ?)", (f"{PREV}T18:30:00", now_ist()))
        conn.execute("INSERT INTO deals (date, symbol, client, side, qty, price, source, pct, after_pct, promoter, url, fetched_at) VALUES "
                     "(?, 'RT', 'BIG FUND', 'SELL', 500000, 960.0, 'block', NULL, NULL, 0, '', ?)", (DAY, now_ist()))
    return ids


# --- what closed, what moved ------------------------------------------------------------------------------------------------------

def test_closed_today_normalises_the_four_books():
    ids = seed_books()
    rows = {t["book"]: t for t in why.closed_today(DAY)}
    assert set(rows) == {"real", "paper", "futures", "options"}
    r = rows["real"]
    assert (r["key"], r["symbol"], r["entry"], r["exit"], r["net"], r["label"], r["stop"]) == (f"real:{ids['real']}", "RT", 1000.0, 940.0, -620.0, "stop hit", 940.0)
    assert rows["paper"]["key"] == f"paper:{ids['paper']}" and rows["paper"]["why"] == "close under SMA50" and rows["paper"]["side"] == "long"
    f = rows["futures"]
    assert f["side"] == "short" and f["extra"] == {"account": "paper", "contract": "FT26NOVFUT", "rolls": 0} and f["qty"] == 500
    o = rows["options"]
    assert o["symbol"] == "NIFTY" and o["extra"]["bias"] == "bear" and o["extra"]["strike"] == 25500 and o["label"] == "premium stop" and o["out"] == "11:20 AM"
    assert why.closed_today("2026-11-10") == []


def test_moved_today_gap_and_stop(cfg):
    hold("GAP", "FLAT", "STOPPED")
    bars("GAP", 100.0, 97.5, 99.0)
    bars("FLAT", 100.0, 100.5, 101.0)
    bars("STOPPED", 100.0, 100.2, 95.0)
    with get_db() as conn:
        conn.execute("INSERT INTO real_orders (key, day, created_at, symbol, action, qty, reason, status) VALUES (?, ?, ?, 'STOPPED', 'EXIT', 5, 'stop 96.0 hit', 'sent')",
                     (f"{DAY}|STOPPED|EXIT", DAY, now_ist()))
    out = {m["symbol"]: m for m in why.moved_today(DAY, cfg)}
    assert set(out) == {"GAP", "STOPPED"}
    assert out["GAP"]["label"] == "gapped -2.5%" and out["GAP"]["extra"]["move"]["close_pct"] == -1.0 and out["GAP"]["key"] == f"held:GAP:{DAY}"
    assert out["STOPPED"]["label"] == "stop hit" and out["STOPPED"]["extra"]["stopped"] is True
    assert why.day_move("FLAT", DAY) == {"close_pct": 1.0, "gap_pct": 0.5, "open": 100.5, "close": 101.0, "prev": 100.0}
    assert why.day_move("NOBARS", DAY) is None


# --- the evidence ------------------------------------------------------------------------------------------------------------------------

def test_evidence_lines_for_a_cash_trade(cfg):
    seed_books()
    t = next(x for x in why.closed_today(DAY) if x["book"] == "real")
    ev = [e["text"] for e in why.evidence(t, DAY, cfg)]
    assert ev[0] == "real trade: 10 from 1,000.00 (2026-11-03) to 940.00 (2026-11-17), stop 940.00, 10 sessions; ended: stop hit; net -620"
    assert ev[1] == "RT today: opened -2.10% vs the previous close, closed -5.00%"
    assert ev[2] == "Chemicals (1 stocks) averaged -1.40% today"
    assert ev[3] == "NIFTY closed +2.40% today"
    assert ev[4].startswith("filing 2026-11-16 · Outcome of Board Meeting: Net loss of Rs. 12.4 crore") and ev[5] == "deal 2026-11-17 · BIG FUND sell 500,000 shares at 960.00 (block)"
    assert [e.get("url") for e in why.evidence(t, DAY, cfg)][4] == "https://nsearchives.nseindia.com/RT.pdf"
    assert "Quote only the numbers above" in why.prompt(t, why.evidence(t, DAY, cfg)) and "Trade: RT · real · stop hit" in why.prompt(t, [])


def test_evidence_for_options_and_futures_and_the_ai_note_title(cfg):
    seed_books()
    rows = {t["book"]: t for t in why.closed_today(DAY)}
    ev = [e["text"] for e in why.evidence(rows["options"], DAY, cfg)]
    assert ev[0] == ("options spread: bear bias, score 2, sold 25,500 CE; credit 60.0, bought back at 92.0; ended: premium stop at 11:20 AM; net -2,450; "
                     "spot 25,400 -> 25,620")
    assert ev[1] == "NIFTY closed +2.40% today" and len(ev) == 2                               # no stock lines for an index spread
    ev = [e["text"] for e in why.evidence(rows["futures"], DAY, cfg)]
    assert ev[0].startswith("futures trade short: 500 from 200.00 (2026-11-12) to 206.00") and ev[1] == "FT today: opened +1.50% vs the previous close, closed +3.00%"
    with get_db() as conn:                                                                      # a filing with an AI note: the note's title stands in
        conn.execute("INSERT INTO ai_notes (created, day, kind, symbol, severity, title, text, data, prompt_version, source_key) VALUES (?, ?, 'filing', 'RT', 'high', "
                     "'Q2 loss widens', 'l', '{}', ?, '77')", (now_ist(), PREV, notes.PROMPT_V))
    ev = [e["text"] for e in why.evidence(rows["real"], DAY, cfg)]
    assert ev[4] == "filing 2026-11-16 · Outcome of Board Meeting: Q2 loss widens"


# --- the line --------------------------------------------------------------------------------------------------------------------------------

def test_validate_line_prefix_numbers_and_brokers():
    ev = "real trade: 10 from 1,000.00 to 940.00; RT today: opened -2.10%, closed -5.00%"
    v = why.validate(json.dumps({"why": "likely cause: the stock fell 5.00% after the loss; the stop at 940.00 gave way."}), ev)
    assert v == {"line": "likely cause: the stock fell 5.00% after the loss; the stop at 940.00 gave way.", "numbers_checked": True}
    v = why.validate(json.dumps({"why": "The stop at 940.00 gave way as the sector fell 3.2%."}), ev)
    assert v["line"].startswith("likely cause: the stop at 940.00 gave way") and "3.2" not in v["line"] and v["numbers_checked"] is False
    assert len(why.validate(json.dumps({"line": "x" * 500}), ev)["line"]) == why.LINE_MAX
    for bad in ("nope", "[]", json.dumps({"why": ""}), json.dumps({"why": "likely cause: sold on " + "Zerodha"})):
        with pytest.raises(why.WhyInvalid):
            why.validate(bad, ev)


# --- the night job ----------------------------------------------------------------------------------------------------------------------------

def test_run_night_writes_rows_and_the_evening_message_once(ollama, cfg):
    ids = seed_books()
    hold("GAP")
    bars("GAP", 100.0, 97.5, 99.0)
    fake = FakeTG()
    out = why.run_night(cfg, NIGHT, Notifier(cfg.alerts, fake))
    assert out == {"tokens": 45, "note": "5 why line(s) · 0 failed"} and len(ollama.calls) == 5
    rows = notes.list_notes(kind="why")["rows"]
    assert rows and {r["symbol"] for r in rows} == {"RT", "PT", "FT", "NIFTY", "GAP"} and all(r["severity"] == "info" and r["prompt_version"] == "why-v1" for r in rows)
    r = next(x for x in rows if x["symbol"] == "RT")
    assert r["title"] == "RT · stop hit" and r["text"].startswith("likely cause: the stock gapped -2.10%") and r["source_key"] == f"real:{ids['real']}"
    assert r["numbers_checked"] is True and r["book"] == "real" and r["alerted"] is True and r["day"] == DAY
    gap = next(x for x in rows if x["symbol"] == "GAP")
    assert gap["book"] == "held" and gap["alerted"] is False and gap["title"] == "GAP · gapped -2.5%"
    body = ollama.calls[0][2]
    assert body["model"] == "qwen2.5:7b-instruct" and body["format"] == "json" and "never advise, grade the rule" in body["system"]
    assert len(fake.sent) == 1
    text = fake.sent[0][1]
    assert text.startswith("🧾 <b>Today's exits, explained</b> · Tue 17 Nov") and "💰 <b>RT</b> · stop hit · −₹620\n   🤖 <i>likely cause:" in text
    assert "🧩 <b>NIFTY</b> · premium stop · −₹2,450" in text and "📐 <b>FT</b>" in text and "GAP" not in text and "no action taken" in text
    assert no_brokers(text)
    assert why.run_night(cfg, NIGHT, Notifier(cfg.alerts, fake)) == {"skip": f"nothing closed or moved on {DAY}"} and len(fake.sent) == 1
    assert why.run_night(cfg, datetime(2026, 11, 10, 21, 0, tzinfo=IST))["skip"].startswith("nothing closed")


def test_run_night_skips_an_item_the_model_cannot_explain(ollama, cfg):
    seed_books()
    ollama.reply = ["bad", "bad", LINE, LINE, LINE, LINE]                                        # the first item: invalid twice -> skipped
    fake = FakeTG()
    out = why.run_night(cfg, NIGHT, Notifier(cfg.alerts, fake))
    assert out["note"] == "3 why line(s) · 1 failed" and len(notes.list_notes(kind="why")["rows"]) == 3 and len(fake.sent) == 1
    assert "<b>RT</b>" not in fake.sent[0][1]


def test_why_switch_off_and_enqueue_dedupe(ollama, cfg):
    cfg.ai.why = False
    assert why.enqueue_tonight(cfg, NIGHT) == {"status": "off"}
    cfg.ai.why = True
    seed_books()
    job = why.enqueue_tonight(cfg, NIGHT)
    assert job["key"] == f"why:{DAY}" and job["dedup"] is False and why.enqueue_tonight(cfg, NIGHT)["dedup"] is True
    assert why.running()
    q.wait(job["id"], 10)
    assert not why.running()


def test_evening_message_without_the_switch_still_writes_rows(ollama, cfg):
    seed_books()
    cfg.ai.why = False
    fake = FakeTG()
    why.run_night(cfg, NIGHT, Notifier(cfg.alerts, fake))                                      # the job was queued before the switch went off
    assert len(notes.list_notes(kind="why")["rows"]) == 4 and fake.sent == []


# --- on the pages ---------------------------------------------------------------------------------------------------------------------------------

def test_attach_on_the_trade_tables(ollama, cfg):
    ids = seed_books()
    why.run_night(cfg, NIGHT, Notifier(cfg.alerts, FakeTG()))
    rows = why.attach([{"id": ids["real"], "symbol": "RT"}, {"id": ids["old"], "symbol": "OLD"}, {"symbol": "X"}], "real")
    assert rows[0]["ai_why"]["text"].startswith("likely cause:") and rows[0]["ai_why"]["id"] and rows[1]["ai_why"] is None and rows[2]["ai_why"] is None
    real._save({"started": "2026-10-01", "capital": 300000.0, "cash": 300000.0, "charges": 0.0, "positions": {}, "orders": [], "cap": 10, "allowed": True})
    with get_db() as conn:
        conn.execute("INSERT INTO real_equity (day, equity, gross, nifty) VALUES (?, 300000, 300000, 25000)", (DAY,))
    s = real.summary(cfg)
    by = {t["id"]: t for t in s["trades"]}
    assert by[ids["real"]]["ai_why"]["text"].startswith("likely cause:") and by[ids["old"]]["ai_why"] is None
    from backend.options import book as obook
    assert obook.trades(5)[0]["id"] == ids["options"] and obook._ai_why(obook.trades(5))[0]["ai_why"]["text"].startswith("likely cause:")
    from backend.futures import book as fbook
    assert fbook._ai_why(fbook._trades())[0]["ai_why"]["text"].startswith("likely cause:")


def test_evening_message_text():
    rows = [{"book": "paper", "symbol": "PT", "text": "likely cause: <b>x</b>", "trade": {"label": "close under SMA50", "net": 390.0}},
            {"book": "futures", "symbol": "FT", "text": "likely cause: y", "data": {"trade": {"label": "stop hit", "net": -3100.0}}}]
    text = msg.ai_why_evening(rows, "2026-11-17")
    assert text.split("\n") == ["🧾 <b>Today's exits, explained</b> · Tue 17 Nov", "📝 <b>PT</b> · close under SMA50 · +₹390", "   🤖 <i>likely cause: &lt;b&gt;x&lt;/b&gt;</i>",
                                "📐 <b>FT</b> · stop hit · −₹3,100", "   🤖 <i>likely cause: y</i>",
                                "<i>written from the trade rows, the day's bars and the filings · no action taken</i>"]


# --- the hook and the route --------------------------------------------------------------------------------------------------------------------------

def test_run_eod_hook_and_the_route(monkeypatch, ollama, cfg, fake_kite, make_client):
    from backend.alerts.jobs import Scheduler
    from tests.test_login import LoginState
    st = LoginState()
    st.config = cfg
    sch = Scheduler(st, notifier_factory=lambda c: Notifier(c, FakeTG()))
    monkeypatch.setattr(sch, "_run_eod", lambda now, force: {"status": "ok"})
    monkeypatch.setattr(notes, "enqueue_tonight", lambda c, now, notifier=None: {"queued": 0})
    monkeypatch.setattr(why, "enqueue_tonight", lambda c, now, notifier=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert sch.run_eod(NIGHT)["ai_why"]["status"] == "failed"                                     # never breaks the evening job
    monkeypatch.undo()
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("KITE_API_KEY", "key123")
    monkeypatch.setenv("KITE_API_SECRET", "sec")
    from fastapi.testclient import TestClient
    from backend import main
    kc = make_client(fake_kite, history_years=1)
    kc.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc_, ic: kc)
    with TestClient(main.app) as tc:
        with get_db() as conn:
            conn.execute("INSERT INTO ai_jobs (created, kind, key, status) VALUES (?, 'night', 'why:x', 'queued')", (now_ist(),))
        assert tc.post("/api/ai/why/run").status_code == 409
        with get_db() as conn:
            conn.execute("UPDATE ai_jobs SET status = 'skipped'")
        job = tc.post("/api/ai/why/run").json()
        assert job["key"].startswith("why:") and job["dedup"] is False
        q.wait(job["id"], 10)
        assert tc.get("/api/ai/notes?kind=why").json() == {"rows": [], "total": 0}
        assert tc.get("/api/ai").json()["settings"]["why"] is True
