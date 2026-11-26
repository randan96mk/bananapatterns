"""💡 Catch: the reader's state machine on a fake Telethon client (live / down with the reason / never a crash), the
preview page fallback, the HTML export import, a day of posts through ingest (hint, chatter, image, follow-ups linked),
edits and deletions, the login flow writing the session to .env (never logged, never returned)."""

import logging
from datetime import timezone
from types import SimpleNamespace

import pytest

from backend.database.models import get_db
from backend.tips import book as bk
from backend.tips import reader as rd
from backend.config import AppConfig
from tests.tips_world import BN12, BN13, at, cfg, posts, seed, world


class St:
    def __init__(self):
        self.config = cfg()
        self.config.tips.enabled = True
        self.client = world()


class FakeTele:
    """The bits of a Telethon client the reader uses."""

    def __init__(self, fail=None, authorised=True, msgs=(), reader=None):
        self.fail, self.authorised, self.msgs, self.reader = fail, authorised, list(msgs), reader
        self.handlers = []

    async def connect(self):
        if self.fail:
            raise self.fail

    async def is_user_authorized(self):
        return self.authorised

    async def get_me(self):
        return SimpleNamespace(phone="919876543210", username="me")

    async def get_entity(self, name):
        return SimpleNamespace(id=1001, username=name)

    async def iter_dialogs(self):
        for d in (SimpleNamespace(title="Family", entity=SimpleNamespace(id=55, title="Family")),
                  SimpleNamespace(title="Index Options, Buying with Aakanksha", entity=SimpleNamespace(id=-1002233445566, title="Index Options, Buying with Aakanksha"))):
            yield d

    async def get_messages(self, ent, limit=20):
        return list(reversed(self.msgs))

    def add_event_handler(self, fn, ev):
        self.handlers.append((fn, type(ev).__name__))

    async def run_until_disconnected(self):
        self.seen_state = rd.status()["state"]
        self.reader._stop.set()

    async def disconnect(self):
        pass


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setenv("TIPS_TG_SESSION", "fake-session")
    monkeypatch.setenv("TIPS_TG_API_ID", "123")
    monkeypatch.setenv("TIPS_TG_API_HASH", "abc")


def msg(mid, text, when, media=None):
    return SimpleNamespace(id=mid, message=text, date=when.astimezone(timezone.utc), media=media)


def test_reader_goes_live_and_catches_up(session):
    st = St()
    fake = FakeTele(msgs=[msg(17605, BN12, at("09:43")), msg(17607, "", at("09:46"), media=object())])
    r = rd.Reader(st, client_factory=lambda: fake)
    fake.reader = r
    r.run_once()
    assert fake.seen_state == "live"
    s = rd.status()
    assert s["account_masked"] == "+91••••••3210" and s["last_post_at"].startswith("2026-11-17T09:4")
    assert sorted(n for _, n in fake.handlers) == ["MessageDeleted", "MessageEdited", "NewMessage"]
    with get_db() as conn:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM tips_posts ORDER BY post_id")]
    assert kinds == ["hint", "image"]


def test_reader_failure_is_down_with_the_reason_never_a_crash(session, monkeypatch):
    st = St()
    r = rd.Reader(st, client_factory=lambda: FakeTele(fail=ConnectionError("no route to host")))
    monkeypatch.setattr(r, "fallback", lambda s: r._stop.set())
    r._run()                                                       # returns; nothing raised
    s = rd.status()
    assert s["state"] == "down" and s["reason"] == "ConnectionError: no route to host"
    r2 = rd.Reader(st, client_factory=lambda: FakeTele(authorised=False))
    monkeypatch.setattr(r2, "fallback", lambda s: r2._stop.set())
    r2._run()
    assert rd.status()["reason"] == "the session is not authorised · log in again"


class BlockingTele(FakeTele):
    """A client that stays connected until disconnect() is called (like Telethon's run_until_disconnected)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.gone = None
        self.disconnects = 0

    async def run_until_disconnected(self):
        import asyncio
        self.gone = asyncio.Event()
        await self.gone.wait()
        raise ConnectionError("Cannot send requests while disconnected")   # what Telethon raised on the server

    async def disconnect(self):
        self.disconnects += 1
        if self.gone is not None:
            self.gone.set()


def test_restart_after_a_channels_change_brings_a_new_thread_up(session, monkeypatch):
    """19 Nov: saving channels called stop() then start(); start() saw the old thread alive and did nothing, the old
    thread died on the disconnect with 'Cannot send requests while disconnected' and the reader stayed down."""
    import time
    st = St()
    clients = []

    def factory():
        c = BlockingTele()
        clients.append(c)
        return c
    r = rd.Reader(st, client_factory=factory)
    assert r.start() is True
    for _ in range(100):
        if rd.status()["state"] == "live":
            break
        time.sleep(0.05)
    assert rd.status()["state"] == "live" and len(clients) == 1
    assert r.restart() is True                                       # the channels changed
    for _ in range(100):
        if rd.status()["state"] == "live" and len(clients) == 2:
            break
        time.sleep(0.05)
    s = rd.status()
    assert s["state"] == "live" and len(clients) == 2 and clients[0].disconnects == 1
    assert "disconnected" not in (s.get("reason") or "")               # the expected disconnect is not a failure
    r.stop()
    r._thread.join(timeout=5)
    assert not r.running


def test_reader_starts_only_when_enabled_with_a_session(monkeypatch):
    monkeypatch.setenv("TIPS_TG_SESSION", "")
    monkeypatch.setattr(rd, "SESSION_FILE", rd.SESSION_FILE.parent / "nope" / ".tips_session")
    st = St()
    assert rd.Reader(st, client_factory=lambda: FakeTele()).start() is False
    assert rd.status()["state"] == "logged_out"
    monkeypatch.setenv("TIPS_TG_SESSION", "x")
    st.config.tips.enabled = False
    assert rd.Reader(st, client_factory=lambda: FakeTele()).start() is False
    assert rd.status() == {**rd.status(), "state": "down", "reason": "Tips switched off"}


PREVIEW = """<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="aakankshatrading/17605">
<div class="tgme_widget_message_text js-message_text" dir="auto">Buy bank nifty 50300 PE near 250-270 when comes <br/>Sl 190<br/>Tgt 320-350-400++</div>
<a class="tgme_widget_message_date"><time datetime="2026-11-17T04:13:01+00:00" class="time">9:43</time></a></div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="aakankshatrading/17607">
<a class="tgme_widget_message_photo_wrap" style="x"></a><time datetime="2026-11-17T04:16:37+00:00" class="time">9:46</time></div></div>"""


def test_preview_fallback_parses_the_public_page(session, monkeypatch):
    st = St()
    got = rd.parse_preview(PREVIEW)
    assert [(p["post_id"], p["has_media"], p["posted_at"].strftime("%H:%M")) for p in got] == [(17605, False, "09:43"), (17607, True, "09:46")]
    assert "Sl 190" in got[0]["text"]
    monkeypatch.setattr(rd, "PREVIEW_EVERY_S", 0)
    r = rd.Reader(st, fetch=lambda url: PREVIEW)
    r.fallback(0.01)
    assert rd.status()["state"] == "preview"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tips_posts WHERE source = 'preview'").fetchone()[0] == 2


EXPORT = """<html><body><div class="page_header"><div class="content"><div class="body"><div class="name bold">Some Channel</div></div></div></div>
<div class="history">
<div class="message default clearfix" id="message17605"><div class="body"><div class="pull_right date details" title="7 August 2024, 09:43:01">09:43</div>
<div class="text">Buy bank nifty 50300 PE near 250-270 when comes <br>Sl 190<br>Tgt 320-350-400++</div></div></div>
<div class="message default clearfix joined" id="message17607"><div class="body"><div class="media_wrap clearfix"><a class="photo_wrap clearfix pull_left" href="p.jpg"></a></div>
<div class="pull_right date details" title="7 August 2024, 09:46:37">09:46</div></div></div>
<div class="message default clearfix joined" id="message17609"><div class="body"><div class="pull_right date details" title="7 August 2024, 10:08:27">10:08</div>
<div class="text">2 targets done ✅✅✅✅</div></div></div>
<div class="message default clearfix joined" id="message17612"><div class="body"><div class="pull_right date details" title="7 August 2024, 10:52:50">10:52</div>
<div class="text">banknifty is very volatile</div></div></div>
</div></body></html>"""


def test_import_of_the_html_export():
    c = cfg()
    r = rd.import_export(c, [("messages.html", EXPORT.encode())], "aakankshatrading")
    assert r["title"] == "Some Channel" and r["posts"] == 4 and r["kinds"] == {"hint": 1, "image": 1, "follow_up": 1, "chatter": 1}
    h = bk.rows()[0]
    assert h["status"] == "expired" and h["untracked_why"] == "history · no quotes" and h["source"] == "import"
    assert h["events"][-1]["text"].startswith('channel: "2 targets done')
    again = rd.import_export(c, [("messages.html", EXPORT.encode())], "aakankshatrading")
    assert again["new"] == 0                                                 # each post once


def test_a_day_of_posts_links_follow_ups(monkeypatch):
    c, w = cfg(), world()
    pushed = []
    monkeypatch.setattr(bk, "push", lambda config, key, html: pushed.append(key) or True)
    monkeypatch.setattr(rd, "PUSH_MAX_AGE_S", 10 ** 9)
    seed(c, w, BN12, "09:43", 17605)
    seed(c, w, BN13, "09:46", 17606)
    day = {p["post_id"]: p for p in posts()}
    out = {}
    for pid in (17607, 17608, 17609, 17610, 17611, 17612):
        p = day[pid]
        when = at(p["posted_at"][11:16])
        out[pid] = rd.ingest(c, "aakankshatrading", pid, when, p["text"], p["has_media"], push=True, model=False, client=w)
    assert out[17607]["kind"] == "image" and out[17612]["kind"] == "chatter"
    assert [out[i]["kind"] for i in (17608, 17609, 17610, 17611)] == ["follow_up"] * 4
    assert out[17608]["hint_id"] == 2 and out[17611]["hint_id"] == 2                # "50 to 90" sits on #13's levels
    assert bk.load(2)["status"] == "paused"
    # 🔕 push_level normal: "50 to 90" (a price) and "2 targets done" on a hint we neither hold nor armed go to the card log only
    assert sum(1 for k in pushed if k.startswith("tips|fu|")) == 2


def test_edit_and_delete_keep_the_rows():
    c, w = cfg(), world()
    hid = seed(c, w, BN12, "09:43", 17605)["hint_id"]
    rd.edited(c, "aakankshatrading", 17605, BN12.replace("Sl 190", "Sl 200"), at("09:44"))
    h = bk.load(hid)
    assert h["stop"]["value"] == 200 and "edited" in h["events"][-1]["text"]
    assert rd.deleted("aakankshatrading", [17605], at("09:50")) == 1
    with get_db() as conn:
        assert conn.execute("SELECT deleted_at FROM tips_posts WHERE post_id = 17605").fetchone()[0].startswith("2026-11-17T09:50")
    assert bk.load(hid) is not None


class SessionPasswordNeededError(Exception):
    pass


class FakeLogin:
    def __init__(self):
        self.session = SimpleNamespace(save=lambda: "SECRET-SESSION-STRING")
        self.signed = []

    async def connect(self):
        pass

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash="h")

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        self.signed.append((code, password))
        if password is None:
            raise SessionPasswordNeededError("2fa")

    async def get_me(self):
        return SimpleNamespace(phone="919876543210")

    async def disconnect(self):
        pass


def test_login_flow_writes_the_session_to_env_only(tmp_path, monkeypatch, caplog):
    env = tmp_path / ".env"
    env.write_text("KITE_API_KEY=x\nTIPS_TG_SESSION=\n")
    monkeypatch.setattr(rd, "ENV_FILE", env)
    monkeypatch.setenv("TIPS_TG_SESSION", "")
    caplog.set_level(logging.DEBUG)
    lg = rd.Login(client_factory=FakeLogin)
    with pytest.raises(LookupError):
        lg.code("12345")
    assert lg.start("+91 98765 43210") == {"sent": True, "phone_masked": "+91••••••3210"}
    assert lg.code("12345") == {"ok": False, "need_password": True}
    r = lg.code("12345", password="pw")
    assert r == {"ok": True, "saved_to": ".env", "account_masked": "+91••••••3210"}
    assert env.read_text() == "KITE_API_KEY=x\nTIPS_TG_SESSION=SECRET-SESSION-STRING\n"
    assert "SECRET-SESSION-STRING" not in caplog.text and "SECRET" not in str(r)
    assert rd.session_string() == "SECRET-SESSION-STRING"
    monkeypatch.delenv("TIPS_TG_API_ID", raising=False)
    monkeypatch.delenv("TIPS_TG_API_HASH", raising=False)
    with pytest.raises(ValueError, match="TIPS_TG_API_ID"):
        rd.Login().start("+919876543210")


def test_status_mask():
    assert rd.mask("+919876543210") == "+91••••••3210" and rd.mask("someone") == "som•••" and rd.mask(None) is None


# --- a private channel by title / id, replies and the plan completed from a follow-up ---------------------------------

def test_a_private_channel_is_found_by_title_or_id():
    import asyncio
    fake = FakeTele()
    assert rd.is_username("aakankshatrading") and not rd.is_username("Index Options Buying with Aakanksha") and not rd.is_username("1002233445566")
    assert asyncio.run(rd.find_channel(fake, "buying with aakanksha")).id == -1002233445566            # part of the title
    assert asyncio.run(rd.find_channel(fake, "Index Options, Buying")).id == -1002233445566    # the start of the title
    assert asyncio.run(rd.find_channel(fake, "1002233445566")).id == -1002233445566
    assert asyncio.run(rd.find_channel(fake, "aakankshatrading")).username == "aakankshatrading"
    with pytest.raises(LookupError):
        asyncio.run(rd.find_channel(fake, "No Such Channel"))
    assert AppConfig().tips.channels == "Index Options Buying with Aakanksha"
    assert asyncio.run(rd.find_channel(FakeTele(), "Index Options Buying with Aakanksha")).id == -1002233445566   # comma or not
    assert rd.split_channels("aakankshatrading, other_channel") == ["aakankshatrading", "other_channel"]
    assert rd.split_channels("Index Options, Buying with Aakanksha; @other\n1002233445566") == ["Index Options, Buying with Aakanksha", "other", "1002233445566"]
    c0 = cfg()
    c0.tips.channels = "Index Options, Buying with Aakanksha"
    assert rd.channels_of(c0) == ["index options, buying with aakanksha"]
    assert asyncio.run(rd.find_channel(FakeTele(), "index options, buying with aakanksha")).id == -1002233445566
    fetched = []
    rd.preview_once(cfg(), fetch=lambda u: fetched.append(u) or "<html></html>")   # the public one only
    c = cfg()
    c.tips.channels = "Index Options, Buying with Aakanksha"
    rd.preview_once(c, fetch=lambda u: fetched.append(u) or "<html></html>")
    assert fetched == ["https://t.me/s/aakankshatrading"]


def test_the_sensex_sequence_posted_as_replies(monkeypatch):
    """19 Nov: the call first, then the SL / targets, ACTIVATED and the booking advice as replies quoting it."""
    c, w = cfg(), world()
    c.tips.kite_default_stop = 0                        # no provisional stop: the plan waits for the channel's SL
    ch = "index options buying with aakanksha"
    pushed = []
    monkeypatch.setattr(bk, "push", lambda config, key, html: pushed.append((key, html)) or True)
    monkeypatch.setattr(rd, "PUSH_MAX_AGE_S", 10 ** 9)
    t = at("10:06")
    r1 = rd.ingest(c, ch, 1, t, "", True, push=True, model=False, client=w)
    r2 = rd.ingest(c, ch, 2, t, "BUY SENSEX 82000 CE NEAR 460\n\n2 LOTS", False, push=True, model=False, client=w)
    assert r1["kind"] == "image" and r2["kind"] == "hint"
    hid = r2["hint_id"]
    h = bk.load(hid)
    assert (h["symbol"], h["strike"], h["opt_type"], h["side"], h["size_hint"]) == ("SENSEX", 82000, "CE", "BUY", {"lots": 2})
    assert h["plan"]["ok"] is False and "stop missing" in h["plan"]["why"]
    r3 = rd.ingest(c, ch, 3, t, "", True, push=True, model=False, client=w, reply_to=2)          # the screenshot
    r4 = rd.ingest(c, ch, 4, t, "CMP 460", False, push=True, model=False, client=w, reply_to=2)
    r5 = rd.ingest(c, ch, 5, at("10:07"), "SL 410\nTGT 520/550/600+", False, push=True, model=False, client=w, reply_to=2)
    r6 = rd.ingest(c, ch, 6, at("10:07"), "ACTIVATED", False, push=True, model=False, client=w, reply_to=2)
    r7 = rd.ingest(c, ch, 7, at("10:07"), "460 TO NOW 505\nDEFENSIVE TRADERS: book part qty and keep TSL at cost for rest qty",
                   False, push=True, model=False, client=w, reply_to=2)
    assert [r["kind"] for r in (r3, r4, r5, r6, r7)] == ["follow_up"] * 5
    assert all(r["hint_id"] == hid for r in (r3, r4, r5, r6, r7))
    assert r5["line"] == "plan completed: stop 410 · targets 520/550/600+"
    h = bk.load(hid)
    assert h["stop"] == {"kind": "premium", "value": 410} and h["targets"] == [520, 550, 600] and h["open_ended"] is True
    assert h["plan"]["ok"] is True and h["plan"]["stop"]["value"] == 410 and h["plan"]["targets"][:3] == [520, 550, 600]
    assert h["run"]["state"] == "waiting" and h["run"]["stop"] == 410 and h["run"].get("allowed") is True   # CMP 460 allowed it
    assert any("plan completed" in e["text"] for e in h["events"])
    assert r6["line"] == "entry allowed at the market" or r6["line"] == "noted"
    with get_db() as conn:
        rows = conn.execute("SELECT post_id, kind, classified_by, reply_to FROM tips_posts WHERE channel = ? ORDER BY post_id", (ch,)).fetchall()
    assert [tuple(r) for r in rows][4] == (5, "follow_up", "reply", 2)
    # ACTIVATED without the reply link still reads as a follow-up by its word
    r8 = rd.ingest(c, ch, 8, at("10:08"), "ACTIVATED", False, push=True, model=False, client=w)
    assert r8["kind"] == "follow_up" and r8["hint_id"] == hid
    # a new call posted as a reply to the old one is a new hint, not a follow-up
    r9 = rd.ingest(c, ch, 9, at("10:30"), "BUY SENSEX 82800 PE NEAR 660\nSL 600\nTGT 720/780", False, push=True, model=False, client=w, reply_to=2)
    assert r9["kind"] == "hint" and r9["hint_id"] != hid
    assert sum(1 for k, _ in pushed if k.startswith("tips|hint|")) == 2
    first = next(html for k, _html in pushed if k == f"tips|hint|{hid}" for html in [_html])
    assert first.startswith(f"💡 <b>Tip #{hid} · index options buying with aakanksha · ") and "10:06 AM</b>" in first and "SENSEX" in first


def test_old_or_broken_channel_values_move_to_the_default():
    for old in ("aakankshatrading", "Index, Options, Buying, with, Aakanksha", "index;options;buying;with;aakanksha", ""):
        c = cfg()
        c.tips.channels = old
        assert rd.migrate_channels(c) is True and c.tips.channels == "Index Options Buying with Aakanksha"
    c = cfg()
    c.tips.channels = "some_other_channel"
    assert rd.migrate_channels(c) is False and c.tips.channels == "some_other_channel"
    c.tips.channels = "Index Options Buying with Aakanksha"
    assert rd.migrate_channels(c) is False


def test_the_sensex_pe_sequence_with_the_plan_in_a_second_post(monkeypatch):
    """18 Nov 1:05 PM: the call, then a second post with the add-on, SL and five targets, then the running commentary."""
    c, w = cfg(), world()
    c.tips.kite_default_stop = 0                        # no provisional stop: the plan waits for the channel's SL
    ch = "index options buying with aakanksha"
    monkeypatch.setattr(bk, "push", lambda *a, **k: True)
    seq = [(1, "13:05", "buy sensex 82400 pe above 685++\n\n2 lots"),
           (2, "13:05", "we will add more near 635-600 after breakout activation \n3-3 lots\nSL 550\ntgt 745/770/800/850/900"),
           (3, "13:07", "active"), (4, "13:07", "tgt 1 done ✅✅✅"), (5, "13:07", "defensive tradersensive traders can book full or trail"),
           (6, "13:07", "hows the josh"), (7, "13:07", "one sided fataaa kya"),
           (8, "13:08", "tgt 2 done \n\ndefensive tradersensive traders can book full at 735 & 770"), (9, "13:08", "795++"), (10, "13:09", "century done ✅✅✅")]
    out = {pid: rd.ingest(c, ch, pid, at(hhmm), text, False, push=False, model=False, client=w) for pid, hhmm, text in seq}
    assert out[1]["kind"] == "hint"
    hid = out[1]["hint_id"]
    assert out[2]["kind"] == "follow_up" and out[2]["hint_id"] == hid
    assert out[2]["line"] == "plan completed: stop 550 · targets 745/770/800/850/900"
    h = bk.load(hid)
    assert (h["symbol"], h["strike"], h["opt_type"], h["side"]) == ("SENSEX", 82400, "PE", "BUY")
    assert h["entry"] == {"mode": "above", "lo": 685, "hi": 685, "when_comes": False, "level_kind": "premium", "wait_words": False,
                          "needs_activation": False} and h["size_hint"] == {"lots": 2}
    assert h["stop"] == {"kind": "premium", "value": 550} and h["targets"] == [745, 770, 800, 850, 900] and h["plan"]["ok"] is True
    assert [out[i]["kind"] for i in (3, 4, 5, 8, 9, 10)] == ["follow_up"] * 6 and all(out[i]["hint_id"] == hid for i in (3, 4, 5, 8, 9, 10))
    assert [out[i]["kind"] for i in (6, 7)] == ["chatter", "chatter"]
    h = bk.load(hid)
    assert h["targets"] == [745, 770, 800, 850, 900] and h["stop"]["value"] == 550      # "tgt 1 done" changed no level
    assert out[3]["line"] == "entry allowed at the market"


def test_a_saved_old_time_stop_moves_to_the_new_default():
    c = cfg()
    c.tips.intraday_time_stop = "15:20"
    assert rd.migrate_time_stop(c) is True and c.tips.intraday_time_stop == "15:14"
    c.tips.intraday_time_stop = "15:05"
    assert rd.migrate_time_stop(c) is False and c.tips.intraday_time_stop == "15:05"
    assert AppConfig().tips.intraday_time_stop == "15:14"


def test_26_nov_bare_sl_and_tgt_posts_complete_the_plan_even_when_the_model_says_chatter(monkeypatch):
    """26 Nov: "Buy sensex 83500 CE above 665 / 2 lots" (no stop), then "Sl 530", "Tgt 830/880/940" and the correction
    "Tgt 730/780/840" as separate posts (no reply). The model had sorted them as chatter and the card stayed
    "no usable stop": code now decides these, the plan completes and the corrected targets win."""
    from backend.tips import filter as flt
    c, w = cfg(), world()
    c.tips.kite_default_stop = 0                        # no provisional stop: the plan waits for the channel's SL
    ch = "index options buying with aakanksha"
    monkeypatch.setattr(rd, "PUSH_MAX_AGE_S", 10 ** 9)
    monkeypatch.setattr(flt, "model_allowed", lambda config: True)
    monkeypatch.setattr(flt, "model_classify", lambda config, text, hints: {"kind": "chatter", "hint_id": None, "model": "fake", "ms": 1})
    r1 = rd.ingest(c, ch, 1, at("09:29"), "Buy sensex 83500 CE above 665\n\n2 lots", False, push=False, model=True, client=w)
    assert r1["kind"] == "hint"
    hid = r1["hint_id"]
    assert bk.load(hid)["plan"]["ok"] is False
    r2 = rd.ingest(c, ch, 2, at("09:30"), "Sl 530", False, push=False, model=True, client=w)
    r3 = rd.ingest(c, ch, 3, at("09:30"), "Tgt 830/880/940", False, push=False, model=True, client=w)
    r4 = rd.ingest(c, ch, 4, at("09:30", sec=30), "Tgt 730/780/840", False, push=False, model=True, client=w)
    assert [r["kind"] for r in (r2, r3, r4)] == ["follow_up"] * 3 and all(r["hint_id"] == hid for r in (r2, r3, r4))
    assert r2["line"].startswith("plan completed: stop 530")
    assert r3["line"] == "plan completed: targets 830/880/940"
    assert r4["line"] == "plan completed: targets revised 830/880/940 → 730/780/840"
    h = bk.load(hid)
    assert h["plan"]["ok"] is True and h["stop"]["value"] == 530 and h["targets"] == [730, 780, 840]
    r5 = rd.ingest(c, ch, 5, at("09:32"), "Tgt 1 done ✅✅✅", False, push=False, model=False, client=w)
    assert r5["kind"] == "follow_up" and bk.load(hid)["targets"] == [730, 780, 840]     # a result never sets a level
    assert flt.plan_post("Tgt 1 done ✅✅✅", [bk.load(hid)], at("09:32")) is None          # a result is never a plan post
    # "Century done" is a result (A1: by code, before the model): a follow-up that sets no level
    r6 = rd.ingest(c, ch, 6, at("09:33"), "Century done", False, push=False, model=True, client=w)
    assert r6["kind"] == "follow_up" and r6["hint_id"] == hid and bk.load(hid)["targets"] == [730, 780, 840]


def test_26_nov_two_calls_on_one_strike_with_bare_size_and_plan_posts(monkeypatch):
    """26 Nov 10:53–11:01 with the model saying chatter: "Buy sensex 85000 pe above 561" · "2 lots" (own line) · "Sl 490 /
    Tgt 650/700/750/800" · "Buy 85000 pe near 520/30" (a new call: no index named, a zone) · "2 lots" (reply) · "Sl 475 /
    Tgt 565/600/635/700" · "Cmp 527" (reply) · "Avoid this" (reply to the first). Each line lands on the right call."""
    from backend.tips import filter as flt
    c, w = cfg(), world()
    c.tips.kite_default_stop = 0                        # no provisional stop: the plan waits for the channel's SL
    ch = "index options buying with aakanksha"
    monkeypatch.setattr(rd, "PUSH_MAX_AGE_S", 10 ** 9)
    monkeypatch.setattr(flt, "model_allowed", lambda config: True)
    monkeypatch.setattr(flt, "model_classify", lambda config, text, hints: {"kind": "chatter", "hint_id": None, "model": "fake", "ms": 1})
    ing = lambda pid, t, txt, rt=None: rd.ingest(c, ch, pid, at(t), txt, False, push=False, model=True, client=w, reply_to=rt)
    r1 = ing(1, "10:53", "Buy sensex 85000 pe above 561")
    r2 = ing(2, "10:53", "2 lots")
    r3 = ing(3, "10:54", "Sl 490\n\nTgt 650/700/750/800")
    r4 = ing(4, "10:59", "Buy 85000 pe near 520/30")
    r5 = ing(5, "10:59", "2 lots", 4)
    r6 = ing(6, "10:59", "Sl 475\n\nTgt 565/600/635/700")
    r7 = ing(7, "11:00", "Cmp 527", 4)
    r8 = ing(8, "11:01", "Avoid this", 1)
    a, b = r1["hint_id"], r4["hint_id"]
    assert r1["kind"] == "hint" and r4["kind"] == "hint" and a != b
    assert (r2["kind"], r2["hint_id"]) == ("follow_up", a) and "2 lots" in r2["line"]
    assert (r3["kind"], r3["hint_id"]) == ("follow_up", a) and r3["line"] == "plan completed: stop 490 · targets 650/700/750/800"
    assert (r5["kind"], r5["hint_id"]) == ("follow_up", b)
    assert (r6["kind"], r6["hint_id"]) == ("follow_up", b) and r6["line"] == "plan completed: stop 475 · targets 565/600/635/700"
    assert (r7["kind"], r7["hint_id"]) == ("follow_up", b) and (r8["kind"], r8["hint_id"]) == ("follow_up", a)
    ha, hb = bk.load(a), bk.load(b)
    assert ha["size_hint"] == {"lots": 2} and ha["plan"]["ok"] and ha["status"] == "paused"
    assert (hb["symbol"], hb["strike"], hb["opt_type"]) == ("SENSEX", 85000, "PE") and hb["entry"]["lo"] == 520 and hb["entry"]["hi"] == 530
    assert hb["size_hint"] == {"lots": 2} and hb["plan"]["ok"] and hb["run"]["allowed"] is True and hb["status"] != "paused"
