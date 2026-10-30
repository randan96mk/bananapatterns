"""Telegram client, messages, schedule logic, holidays, dispatch (dedup), evening job and intraday heads-up."""

import json
from datetime import date, datetime, timedelta

import httpx
import pytest

from backend.alerts import messages as msg
from backend.alerts.dispatch import Notifier, dispatch_scan
from backend.alerts.jobs import Scheduler, due_jobs, volume_pace
from backend.alerts.telegram import TelegramClient, TelegramError
from backend.config import IST, AlertsConfig, AppConfig, BaseConfig, ScreenConfig
from backend.database.models import get_db, upsert_candles
from backend.engine.scan import run_scan
from backend.ingest import holidays as hol
from tests.test_bases import build


def ist(*a):
    return datetime(*a, tzinfo=IST)


# --- Telegram client -------------------------------------------------------

def tg(handler):
    return TelegramClient(token="T", http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)


def test_send_message_and_retry_after_429():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "description": "Too Many Requests",
                                             "parameters": {"retry_after": 1}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    assert tg(handler).send_message("42", "<b>hi</b>") == {"message_id": 1}
    body = dict(x.split("=") for x in calls[-1].content.decode().split("&"))
    assert len(calls) == 2 and body["chat_id"] == "42" and body["parse_mode"] == "HTML"
    assert "/botT/sendMessage" in str(calls[-1].url)


def test_unauthorised_is_not_retried():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    with pytest.raises(TelegramError, match="Unauthorized"):
        tg(handler).send_message("42", "x")
    assert len(calls) == 1


def test_missing_token():
    with pytest.raises(TelegramError, match="TELEGRAM_BOT_TOKEN"):
        TelegramClient(token="").send_message("1", "x")


def test_detect_chats():
    upd = [{"message": {"chat": {"id": 123, "type": "private", "first_name": "Gagan"}}},
           {"message": {"chat": {"id": 123, "type": "private", "first_name": "Gagan"}}}]
    chats = tg(lambda r: httpx.Response(200, json={"ok": True, "result": upd})).detect_chats()
    assert chats == [{"chat_id": "123", "type": "private", "name": "Gagan"}]


# --- messages ---------------------------------------------------------------

def test_messages_escape_and_digest_chunks():
    p = {"symbol": "A&B", "rs": 90, "tt_passed": 7, "tt_of": 8, "industry": "<IT>", "close": 100.0,
         "vcp": {"stop": 92.0, "contractions": []}}
    html = msg.stage_alert("breakout", "vcp", "A&B broke out <now>", p, 0.8)
    assert "A&amp;B" in html and "&lt;now&gt;" in html and "8.0% risk" in html and "&lt;IT&gt;" in html
    moves = [{"kind": "forming", "text": f"S{i} is newly forming B1 · 5w: 2.0% under the ₹100.00 pivot.",
              "close": 98.0, "change_pct": 1.0} for i in range(120)]
    chunks = msg.digest("vcp", "2026-10-23", {"forming": 120}, moves)
    assert len(chunks) > 1 and all(len(c) < 4096 for c in chunks)
    assert sum(c.count("newly forming") for c in chunks) == 120


def test_due_jobs():
    cfg = AlertsConfig(login_links=False, top_setups=False, morning_plan=False, gap_check=False,
                       filings=False)                                                # tested elsewhere
    assert due_jobs(ist(2026, 10, 23, 15, 40), cfg, set(), True) == ["reminder"]
    assert due_jobs(ist(2026, 10, 23, 18, 31), cfg, set(), True) == ["eod"]
    assert due_jobs(ist(2026, 10, 23, 18, 31), cfg, {"eod"}, True) == []
    assert due_jobs(ist(2026, 10, 20, 18, 31), cfg, set(), False) == []            # holiday
    nb = {"eod": ist(2026, 10, 23, 19, 0)}
    assert due_jobs(ist(2026, 10, 23, 18, 45), cfg, set(), True, not_before=nb) == []
    assert due_jobs(ist(2026, 10, 23, 11, 0), cfg, set(), True) == ["intraday"]
    assert due_jobs(ist(2026, 10, 23, 11, 0), cfg, set(), True, last_intraday=ist(2026, 10, 23, 10, 57)) == []
    assert due_jobs(ist(2026, 10, 25, 10, 5), cfg, set(), False) == ["weekly"]     # Sunday
    assert due_jobs(ist(2026, 10, 23, 18, 31), AlertsConfig(job_enabled=False, login_links=False, morning_plan=False,
                                                            filings=False), set(), True) == []


def test_volume_pace():
    # by 12:30 a typical NSE day has traded half its volume: 1x the average so far -> on pace for 2x
    assert volume_pace(1000, 1000, ist(2026, 10, 23, 12, 30)) == pytest.approx(2.0, rel=0.01)
    assert volume_pace(1000, 1000, ist(2026, 10, 23, 9, 16)) is None              # too early to project


def test_holidays_and_nse_refresh(tmp_path):
    assert not hol.is_trading_day(date(2026, 10, 20)) and hol.is_trading_day(date(2026, 10, 23))
    payload = {"CM": [{"tradingDate": "26-Jan-2027", "description": "Republic Day"}]}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    f = tmp_path / "h.json"
    f.write_text(json.dumps({"holidays": [{"date": "2026-12-25", "description": "Christmas"}]}))
    assert hol.refresh_from_nse(f, client) == 1
    dates = [h["date"] for h in json.loads(f.read_text())["holidays"]]
    assert dates == ["2026-12-25", "2027-01-26"]                                  # merged, not replaced
    hol._cache = None


# --- dispatch + jobs ------------------------------------------------------------

class FakeTG:
    configured = True

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_message(self, chat, html, buttons=None, silent=False):
        if self.fail:
            raise TelegramError("boom")
        self.sent.append(("msg", html))
        self.buttons, self.silent = buttons, silent

    def send_photo(self, chat, png, html, buttons=None, silent=False):
        self.sent.append(("photo", html))
        self.buttons, self.silent = buttons, silent

    def send_album(self, chat, photos, silent=True):
        self.sent.append(("album", [cap for _, cap in photos]))


UP = ("trend", 60, 0.60)
BASE = ("range", 25, (0.92, 0.99), 600_000)


def scanned_db():
    """Three stocks scanned on the same last date: FRESH broke out today, NEAR forms close under its pivot."""
    series = {"FRESH": (1, build([UP, BASE, ("bo", 1, 1.05, 3_000_000)])),
              "NEAR": (2, build([UP, BASE, ("bar", 1, 1.035)])),
              "NONE": (3, build([("trend", 86, -0.2)]))}
    with get_db() as conn:
        for tok, c in series.values():
            upsert_candles(conn, tok, c)
    syms = [{"symbol": k, "instrument_token": v[0]} for k, v in series.items()]
    out = run_scan(BaseConfig(reset_ma=0), ScreenConfig(trend_ma=0), syms)
    return out["as_of"], {k: v[0] for k, v in series.items()}


def test_dispatch_sends_once_and_respects_toggles():
    as_of, tokens = scanned_db()
    cfg = AlertsConfig(chat_id="42", tiering=False, screens=["bases"], near_pivot_pct=0.05)
    fake = FakeTG()
    sent = dispatch_scan(as_of, cfg, BaseConfig(reset_ma=0), Notifier(cfg, fake), tokens)
    kinds = [k for k, _ in fake.sent]
    assert sent["stage"] >= 1 and sent["digest"] == 1 and "photo" in kinds         # breakout comes with a chart
    assert any("FRESH" in h and "broke out" in h for _, h in fake.sent)
    n = len(fake.sent)
    dispatch_scan(as_of, cfg, BaseConfig(reset_ma=0), Notifier(cfg, fake), tokens)
    assert len(fake.sent) == n                                                     # nothing sent twice
    quiet = FakeTG()
    off = AlertsConfig(chat_id="42", tiering=False, screens=["bases"], breakout=False, digest=False, near_pivot=False, forming=False)
    dispatch_scan(as_of, off, BaseConfig(reset_ma=0), Notifier(off, quiet), tokens)
    assert not any("broke out" in h for _, h in quiet.sent)


def test_failed_send_is_retried_next_time():
    as_of, tokens = scanned_db()
    cfg = AlertsConfig(chat_id="42", tiering=False, screens=["bases"], digest=True, near_pivot=False)
    dispatch_scan(as_of, cfg, BaseConfig(reset_ma=0), Notifier(cfg, FakeTG(fail=True)), tokens)
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sent_alerts WHERE ok = 0").fetchone()[0] >= 1
    ok = FakeTG()
    dispatch_scan(as_of, cfg, BaseConfig(reset_ma=0), Notifier(cfg, ok), tokens)
    assert ok.sent


def test_not_configured_sends_nothing():
    as_of, tokens = scanned_db()
    cfg = AlertsConfig(chat_id="")
    assert dispatch_scan(as_of, cfg, BaseConfig(), Notifier(cfg, FakeTG()), tokens)["skipped_ready"]


class FakeState:
    def __init__(self, token=True, snap_status="ok"):
        self.config = AppConfig()
        self.config.alerts = AlertsConfig(chat_id="42", tiering=False, screens=["bases"])
        self.config.bases = BaseConfig(reset_ma=0)
        self.config.screen = ScreenConfig(trend_ma=0)
        self._token, self.worker, self.snap_status = token, None, snap_status
        self.client = None

    def token_valid(self):
        return self._token

    @property
    def live(self):                       # as AppState.live: opening ranges / sparklines cache
        if not hasattr(self, "_live"):
            from backend.alerts.live import LiveBoard
            self._live = LiveBoard(self)
        return self._live

    def run_sync_blocking(self):
        return {"status": self.snap_status, "message": "m", "failed": 0, "errors": []}


def test_eod_waits_for_login_then_runs(monkeypatch):
    as_of, tokens = scanned_db()
    fake = FakeTG()
    st = FakeState(token=False)
    sch = Scheduler(st, notifier_factory=lambda cfg: Notifier(cfg, fake))
    monkeypatch.setattr(Scheduler, "_tokens", lambda self: tokens)
    day = date.fromisoformat(as_of)
    r = sch.run_eod(datetime.combine(day, datetime.min.time(), IST) + timedelta(hours=18, minutes=31))
    assert r["status"] == "waiting_login" and any("login missing" in h for _, h in fake.sent)
    assert "eod" in sch.not_before
    st._token = True
    r = sch.run_eod(datetime.combine(day, datetime.min.time(), IST) + timedelta(hours=18, minutes=50))
    assert r["status"] == "ok" and r["alerts"]["stage"] >= 1
    assert "eod" in Scheduler.done_today(day)


def test_eod_retries_when_no_bar_for_today(monkeypatch):
    as_of, tokens = scanned_db()
    st = FakeState()
    sch = Scheduler(st, notifier_factory=lambda cfg: Notifier(cfg, FakeTG()))
    monkeypatch.setattr(Scheduler, "_tokens", lambda self: tokens)
    later = date.fromisoformat(as_of) + timedelta(days=1)
    r = sch.run_eod(datetime.combine(later, datetime.min.time(), IST) + timedelta(hours=18, minutes=31))
    assert r["status"] == "no_bar" and "eod" not in Scheduler.done_today(later)


def test_intraday_heads_up_once_per_day(monkeypatch):
    as_of, tokens = scanned_db()
    fake = FakeTG()

    class Q:
        def quote(self, keys):
            return {k: {"last_price": 10_000.0, "volume": 5_000_000,                    # far above any pivot
                        "ohlc": {"open": 9_900.0, "high": 10_050.0, "low": 9_850.0}} for k in keys}
    st = FakeState()
    st.client = Q()
    monkeypatch.setattr("backend.alerts.jobs.uni.resolve_universe",
                        lambda cfg: {"stocks": [{"symbol": s, "instrument_token": t} for s, t in tokens.items()]})
    sch = Scheduler(st, notifier_factory=lambda cfg: Notifier(cfg, fake))
    now = ist(2026, 10, 26, 11, 0)
    r = sch.run_intraday(now)
    assert r["sent"] >= 1 and any("Unconfirmed" in h for _, h in fake.sent)
    assert any(k == "photo" and "Verdict:" in h for k, h in fake.sent)                # live decision chart
    assert sch.run_intraday(now + timedelta(minutes=5))["sent"] == 0
