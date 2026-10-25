"""Kite login links: one-time links, callback, schedule, bot commands, dashboard password."""

import base64
from datetime import datetime, timedelta

import pytest

from backend.alerts.commands import BotCommands
from backend.alerts.dispatch import Notifier
from backend.alerts.jobs import Scheduler, due_jobs
from backend.config import IST, AlertsConfig, AppConfig, KiteConfig
from backend.database.models import get_db
from backend.kite_api import auth
from backend.kite_api import login_link as ll
from backend.kite_api.client import KiteAuthError, KiteError
from tests.test_alerts import FakeTG
from tests.test_api import api  # noqa: F401  (fixture)

LOGIN = "https://kite.zerodha.com/connect/login?v=3&api_key=key123"


def ist(*a):
    return datetime(*a, tzinfo=IST)


def nonce_of(url):
    return url.split("redirect_params=n%3D")[1]


# --- one-time links ------------------------------------------------------------

def test_link_carries_nonce_and_works_once():
    now = ist(2026, 10, 26, 6, 1)
    with get_db() as conn:
        url, expires = ll.create_link(conn, LOGIN, now=now)
        n = nonce_of(url)
        assert url.startswith(LOGIN + "&redirect_params=n%3D") and expires == now + timedelta(minutes=15)
        assert ll.check_link(conn, n, now) is None
        assert ll.check_link(conn, n, now + timedelta(minutes=16)) == "expired"
        ll.mark_used(conn, n, now)
        assert ll.check_link(conn, n, now) == "used"
        assert ll.check_link(conn, "nope", now) == "unknown"


def test_newer_link_supersedes_older():
    with get_db() as conn:
        old = nonce_of(ll.create_link(conn, LOGIN)[0])
        new = nonce_of(ll.create_link(conn, LOGIN)[0])
        assert ll.check_link(conn, old) == "superseded" and ll.check_link(conn, new) is None


def test_public_base_url(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("KITE_REDIRECT_URL", raising=False)
    assert ll.redirect_url() == "http://127.0.0.1:8000/api/kite/callback"
    monkeypatch.setenv("KITE_REDIRECT_URL", "http://localhost:8000/api/kite/callback")
    assert ll.public_base_url() == "http://localhost:8000"
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://bp.example.com/")
    assert ll.redirect_url() == "https://bp.example.com/api/kite/callback"


# --- callback ------------------------------------------------------------------

def test_callback_with_telegram_link(api):  # noqa: F811
    tc, _ = api
    with get_db() as conn:
        n = nonce_of(ll.create_link(conn, LOGIN)[0])
    r = tc.get(f"/api/kite/callback?request_token=abc&status=success&n={n}")
    assert r.status_code == 200 and "Kite connected" in r.text and "AlgoTrade" not in r.text
    with get_db() as conn:
        assert auth.load_fresh_token(conn) == "tok-abc"
    again = tc.get(f"/api/kite/callback?request_token=abc&status=success&n={n}")
    assert again.status_code == 400 and "already used" in again.text
    s = tc.get("/api/kite/status").json()
    assert s["has_token"] and s["redirect_url"].endswith("/api/kite/callback") and "algotrade_url" not in s


def test_callback_stale_link_and_cancelled_login(api):  # noqa: F811
    tc, _ = api
    with get_db() as conn:
        old = nonce_of(ll.create_link(conn, LOGIN)[0])
        new = nonce_of(ll.create_link(conn, LOGIN)[0])
    r = tc.get(f"/api/kite/callback?request_token=abc&status=success&n={old}")
    assert r.status_code == 400 and "newer link" in r.text
    assert tc.get(f"/api/kite/callback?status=failed&n={new}").status_code == 400
    assert not tc.get("/api/kite/status").json()["has_token"]


def test_dashboard_password(api, monkeypatch):  # noqa: F811
    tc, _ = api
    monkeypatch.setenv("BP_DASHBOARD_PASSWORD", "s3cret")
    good = {"Authorization": "Basic " + base64.b64encode(b"me:s3cret").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"me:nope").decode()}
    assert tc.get("/health").status_code == 200
    r = tc.get("/api/kite/status")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert tc.get("/api/kite/status", headers=bad).status_code == 401
    assert tc.get("/api/kite/status", headers=good).json()["password_set"] is True
    # dashboard login (no nonce) needs the password; a Telegram link carries its own proof
    assert tc.get("/api/kite/callback?request_token=abc&status=success", follow_redirects=False).status_code == 401
    with get_db() as conn:
        n = nonce_of(ll.create_link(conn, LOGIN)[0])
    assert tc.get(f"/api/kite/callback?request_token=abc&status=success&n={n}").status_code == 200


def test_login_times_validated(api):  # noqa: F811
    tc, _ = api
    r = tc.put("/api/config/alerts", json={"login_times": ["05:30"]})
    assert r.status_code == 422 and "before 06:00" in r.json()["detail"]
    assert tc.get("/api/config/alerts").json()["login_times"] == ["06:01", "08:45"]       # rolled back
    assert tc.put("/api/config/alerts", json={"login_heads_up_time": "5.45"}).status_code == 422
    assert tc.put("/api/config/alerts", json={"login_times": ["06:05", "09:00"]}).status_code == 200


# --- schedule ------------------------------------------------------------------

def test_due_login_slots():
    cfg = AlertsConfig()
    assert "login" not in due_jobs(ist(2026, 10, 26, 6, 0, 30), cfg, set(), True)
    assert "login" in due_jobs(ist(2026, 10, 26, 6, 1), cfg, set(), True)
    later = due_jobs(ist(2026, 10, 26, 7, 0), cfg, {"login@06:01"}, True)
    assert "login" not in later and "login_check" in later
    assert "login" not in due_jobs(ist(2026, 10, 25, 7, 0), cfg, set(), False)              # Sunday
    assert "login" in due_jobs(ist(2026, 10, 25, 7, 0), AlertsConfig(login_every_day=True), set(), False)
    assert "login" not in due_jobs(ist(2026, 10, 26, 7, 0), AlertsConfig(login_links=False), set(), True)
    assert "login_check" not in due_jobs(ist(2026, 10, 26, 7, 0), cfg, {"login@06:01"}, True,
                                         last_login_check=ist(2026, 10, 26, 6, 50))
    assert "login_check" not in due_jobs(ist(2026, 10, 26, 23, 30), cfg, {"login@06:01", "login@08:45"}, True)


def test_due_heads_up():
    on = AlertsConfig(login_heads_up=True)
    assert "login_headsup" in due_jobs(ist(2026, 10, 26, 5, 46), on, set(), True)
    assert "login_headsup" not in due_jobs(ist(2026, 10, 26, 5, 46), AlertsConfig(), set(), True)     # off by default
    assert "login_headsup" not in due_jobs(ist(2026, 10, 26, 6, 2), on, set(), True)                 # too late
    assert "login_headsup" not in due_jobs(ist(2026, 10, 26, 5, 46), on, {"login_headsup"}, True)


class FakeClient:
    def __init__(self, has_token=True, profile_error=None):
        self.has_token, self.profile_error, self.profile_calls = has_token, profile_error, 0

    def login_url(self):
        return LOGIN

    def profile(self):
        self.profile_calls += 1
        if self.profile_error:
            raise self.profile_error
        return {"user_id": "AB1234"}

    def clear_access_token(self):
        self.has_token = False


class LoginState:
    def __init__(self, **client):
        self.config = AppConfig()
        self.config.alerts = AlertsConfig(chat_id="42", intraday=False, filings=False)
        self.config.kite = KiteConfig(api_key="key123", api_secret="sec")
        self.client = FakeClient(**client)
        self.token_source = "db"

    def token_valid(self):
        return self.client.has_token


def scheduler(state, fake):
    return Scheduler(state, notifier_factory=lambda cfg: Notifier(cfg, fake))


def test_missed_slots_send_one_link():
    fake, st = FakeTG(), LoginState(has_token=False)
    sch = scheduler(st, fake)
    assert "login" in sch.tick(ist(2026, 10, 26, 9, 0))          # Mac woke at 09:00: 06:01 and 08:45 both due
    assert len(fake.sent) == 1 and "New Kite day" in fake.sent[0][1]
    label, url = fake.buttons[0]
    assert label == "🔑 Log in to Kite" and url.startswith(LOGIN + "&redirect_params=")
    assert "login" not in sch.tick(ist(2026, 10, 26, 9, 1)) and len(fake.sent) == 1


def test_second_slot_says_still_needed():
    fake, st = FakeTG(), LoginState(has_token=False)
    sch = scheduler(st, fake)
    sch.tick(ist(2026, 10, 26, 6, 2))
    sch.tick(ist(2026, 10, 26, 8, 46))
    assert len(fake.sent) == 2 and "Still not logged in" in fake.sent[1][1]
    with get_db() as conn:                                         # the 06:01 link is now dead
        states = [r[0] for r in conn.execute("SELECT superseded FROM login_links ORDER BY created_at")]
    assert states == [1, 0]


def test_valid_session_sends_nothing():
    fake, st = FakeTG(), LoginState()
    scheduler(st, fake).tick(ist(2026, 10, 26, 6, 2))
    assert fake.sent == [] and st.client.profile_calls == 1


def test_network_error_does_not_send():
    fake, st = FakeTG(), LoginState(profile_error=KiteError("timeout"))
    scheduler(st, fake).tick(ist(2026, 10, 26, 6, 2))
    assert fake.sent == [] and st.client.has_token


def test_revoked_session_gets_link_mid_day():
    fake, st = FakeTG(), LoginState(profile_error=KiteAuthError("TokenException"))
    sch = scheduler(st, fake)
    jobs = sch.tick(ist(2026, 10, 26, 11, 0))      # slots catch up: Kite rejects the held token
    assert "login" in jobs and len(fake.sent) == 1 and not st.client.has_token
    st.client.has_token, st.client.profile_error = True, KiteAuthError("TokenException")
    assert sch.tick(ist(2026, 10, 26, 11, 31)) == ["login_check"]
    assert len(fake.sent) == 2 and "rejected" in fake.sent[1][1]


def test_heads_up_only_with_active_session():
    fake, st = FakeTG(), LoginState()
    st.config.alerts.login_heads_up = True
    scheduler(st, fake).tick(ist(2026, 10, 26, 5, 46))
    assert len(fake.sent) == 1 and "ends at 06:00" in fake.sent[0][1] and "06:01" in fake.sent[0][1]


def test_evening_reminder_carries_link():
    fake, st = FakeTG(), LoginState(has_token=False)
    sch = scheduler(st, fake)
    sch.run_reminder(ist(2026, 10, 26, 15, 31))
    assert len(fake.sent) == 1 and "evening scan" in fake.sent[0][1] and fake.buttons


# --- bot commands --------------------------------------------------------------

class BotTG(FakeTG):
    def __init__(self, updates):
        super().__init__()
        self.updates = updates

    def get_updates(self, offset=None, timeout=20):
        out = [u for u in self.updates if offset is None or u["update_id"] >= offset]
        return out


def update(uid, text, chat=42, at=None):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text,
                                          "date": int((at or ist(2026, 10, 26, 10, 0)).timestamp())}}


def test_login_command_only_from_own_chat():
    now = ist(2026, 10, 26, 10, 0, 30)
    fake = BotTG([update(1, "/login"), update(2, "/login", chat=999), update(3, "hello"),
                  update(4, "/login", at=now - timedelta(minutes=10)), update(5, "/status@my_bot")])
    st = LoginState(has_token=False)
    bot = BotCommands(st, clock=lambda: now)
    n = Notifier(st.config.alerts, fake)
    assert bot.poll_once(n) == 2 and bot.offset == 6
    assert "Login link as requested" in fake.sent[0][1] and "No Kite session" in fake.sent[1][1]
    assert bot.poll_once(n) == 0                                   # nothing new


def test_status_command_with_session():
    now = ist(2026, 10, 26, 10, 0, 30)
    with get_db() as conn:
        auth.store_token(conn, "t", ist(2026, 10, 26, 6, 5))
    fake, st = BotTG([update(1, "/status")]), LoginState()
    BotCommands(st, clock=lambda: now).poll_once(Notifier(st.config.alerts, fake))
    assert "session OK until 06:00 27 Oct" in fake.sent[0][1]


@pytest.mark.parametrize("cmd", ["/help", "/start"])
def test_help(cmd):
    fake, st = BotTG([update(1, cmd)]), LoginState()
    BotCommands(st, clock=lambda: ist(2026, 10, 26, 10, 0, 30)).poll_once(Notifier(st.config.alerts, fake))
    assert "/login" in fake.sent[0][1]
