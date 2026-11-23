"""↗ Tips relay: every NEW channel post forwarded as-is from the owner's own account (the reader's Telethon client) to
the people in tips.relay_to. Never on edits, the catch-up, a post already stored or the rehearsal channel; a failing
entry is one log line + one owner line a day, the others still get the forward; each entry resolved once per process."""

import asyncio
import logging
from datetime import timezone
from types import SimpleNamespace

import pytest

from backend.config import AppConfig, split_relay
from backend.tips import book as bk
from backend.tips import reader as rd
from tests.tips_world import at, cfg, world


class ChatForwardsRestrictedError(Exception):
    pass


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"A wait of {seconds} seconds is required")
        self.seconds = seconds


class St:
    def __init__(self, relay=True, relay_to="@friend_one\n+919876543210"):
        self.config = cfg()
        self.config.tips.enabled = True
        self.config.tips.relay = relay
        self.config.tips.relay_to = relay_to
        self.client = world()


def msg(mid, text, when):
    return SimpleNamespace(id=mid, message=text, date=when.astimezone(timezone.utc), media=None)


class FakeTele:
    """The reader's Telethon client: the channel resolves to id 1001; relay peers resolve per `peers`; forwards are
    recorded. `live` are the posts that arrive once live, `edits` the edits."""

    def __init__(self, reader=None, catch_up=(), live=(), edits=(), fail_entity=(), fail_forward=None):
        self.reader, self.catch_up, self.live, self.edits = reader, list(catch_up), list(live), list(edits)
        self.fail_entity, self.fail_forward = set(fail_entity), dict(fail_forward or {})
        self.handlers, self.forwards, self.resolved = {}, [], []

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(phone="919876543210", username="me")

    async def get_entity(self, name):
        if name == "aakankshatrading":
            return SimpleNamespace(id=1001, username=name)
        self.resolved.append(name)
        if name in self.fail_entity:
            raise ValueError(f'Cannot find any entity corresponding to "{name}"')
        return SimpleNamespace(id=5000 + len(self.resolved), name=name)

    async def get_dialogs(self):
        return []

    async def get_messages(self, ent, limit=20):
        return list(reversed(self.catch_up))

    def add_event_handler(self, fn, ev):
        self.handlers[type(ev).__name__] = fn

    async def forward_messages(self, peer, m):
        exc = self.fail_forward.get(peer.name)
        if exc:
            raise exc
        self.forwards.append((peer.name, m.id))

    async def run_until_disconnected(self):
        for m in self.live:
            await self.handlers["NewMessage"](SimpleNamespace(chat_id=1001, message=m))
        for m in self.edits:
            await self.handlers["MessageEdited"](SimpleNamespace(chat_id=1001, message=m))
        pending = list(self.reader._tasks)
        if pending:
            await asyncio.gather(*pending)
        self.reader._stop.set()

    async def disconnect(self):
        pass


@pytest.fixture(autouse=True)
def session(monkeypatch):
    monkeypatch.setenv("TIPS_TG_SESSION", "fake-session")
    monkeypatch.setenv("TIPS_TG_API_ID", "123")
    monkeypatch.setenv("TIPS_TG_API_HASH", "abc")
    monkeypatch.setattr(rd, "_relay_peers", {})
    monkeypatch.setattr(rd, "_relay_warned", set())
    pushed = []
    monkeypatch.setattr(bk, "push", lambda config, key, html: pushed.append((key, html)) or True)
    return pushed


def run(st, **kw) -> FakeTele:
    r = rd.Reader(st, client_factory=lambda: fake)
    fake = FakeTele(reader=r, **kw)
    r.run_once()
    return fake


def test_on_forwards_a_new_post_to_each_entry_once():
    st = St()
    fake = run(st, catch_up=[msg(100, "good morning all", at("09:10"))], live=[msg(101, "market looks strong today", at("09:30"))])
    assert fake.forwards == [("@friend_one", 101), ("+919876543210", 101)]
    assert rd.status()["relayed_today"] == 2


def test_off_forwards_nothing():
    fake = run(St(relay=False), live=[msg(101, "market looks strong today", at("09:30"))])
    assert fake.forwards == [] and fake.resolved == []
    assert rd.status()["relayed_today"] == 0


def test_edits_catch_up_a_stored_post_and_the_rehearsal_are_never_forwarded():
    st = St()
    old = msg(100, "good morning all", at("09:10"))
    fake = run(st, catch_up=[old], live=[old], edits=[msg(100, "good morning all (edited)", at("09:10"))])
    assert fake.forwards == []                                   # the catch-up, the same post again, the edit
    r = rd.Reader(st)
    assert r._relay_soon("rehearsal", msg(7, "x", at("09:30")), {"new": True}) is None


def test_a_failing_entry_is_one_log_line_and_one_owner_line_a_day_the_others_still_go(session, caplog):
    st = St(relay_to="@good_one\n@blocked_one\n+911234567890\n@busy_one")
    with caplog.at_level(logging.WARNING, logger="tips"):
        fake = run(st, live=[msg(101, "post one here", at("09:30")), msg(102, "post two here", at("09:31"))],
                   fail_entity=["+911234567890"],
                   fail_forward={"@blocked_one": ChatForwardsRestrictedError("You can't forward messages from a protected chat"),
                                 "@busy_one": FloodWaitError(37)})
    assert fake.forwards == [("@good_one", 101), ("@good_one", 102)]
    lines = [r.getMessage() for r in caplog.records if "Tips relay to" in r.getMessage()]
    assert sum("@blocked_one" in ln for ln in lines) == 2         # one log line per failure
    assert any("@busy_one failed: flood wait 37 s" in ln for ln in lines)
    assert sorted(h for _, h in session) == [
        "↗ relay to +911234567890 failed: unknown user (a phone number must be in your contacts)",
        "↗ relay to @blocked_one failed: the channel restricts forwarding",
        "↗ relay to @busy_one failed: flood wait 37 s · skipped"]   # once per entry per day, not per post
    assert rd.status()["relayed_today"] == 2


def test_each_entry_is_resolved_once_per_process():
    st = St()
    fake = run(st, live=[msg(101, "post one here", at("09:30")), msg(102, "post two here", at("09:31"))])
    assert sorted(fake.resolved) == ["+919876543210", "@friend_one"]
    fake2 = run(st, live=[msg(103, "post three here", at("09:32"))])   # a reconnect: the cache still holds them
    assert fake2.resolved == [] and len(fake2.forwards) == 2


def test_settings_parse_the_three_entry_forms():
    assert split_relay("@friend_one\n+91 98765-43210; 123456789\nplain_name\n\n") == \
        ["@friend_one", "+919876543210", "123456789", "@plain_name"]
    assert split_relay("group: vijay and gagan\nChat:Family 2") == ["group: vijay and gagan", "group: Family 2"]
    with pytest.raises(ValueError):
        split_relay("group:")
    c = AppConfig()
    out = c.update_section("tips", {"relay": "true", "relay_to": "@friend_one;+91 98765 43210\n-1001234567"})
    assert out["relay"] is True and out["relay_to"] == "@friend_one\n+919876543210\n-1001234567"
    with pytest.raises(ValueError):
        c.update_section("tips", {"relay_to": "not a person!"})
    assert c.tips.relay_to == "@friend_one\n+919876543210\n-1001234567"   # rolled back
    assert AppConfig().tips.relay is False and AppConfig().tips.relay_to == ""


def test_a_group_entry_resolves_by_title_among_the_dialogs(monkeypatch):
    """"group: <name>" forwards to a group the account is in: resolved through the dialogs like a channel title."""
    import asyncio
    from types import SimpleNamespace
    from backend.tips import reader as rd

    class Tele:
        def __init__(self):
            self.forwards = []

        async def iter_dialogs(self):
            for t, i in (("Moltbot", 7), ("vijay and gagan", -1009001), ("GAGAN AND VIJAY", -1009002)):
                yield SimpleNamespace(title=t, entity=SimpleNamespace(id=i, title=t))

        async def get_entity(self, x):
            raise AssertionError("a group entry never goes through get_entity")

    rd._relay_peers.clear()
    peer = asyncio.run(rd._relay_peer(Tele(), "group: vijay and gagan"))
    assert peer.id == -1009001
    assert rd._relay_peers["group: vijay and gagan"] is peer
    with pytest.raises(LookupError):
        asyncio.run(rd._relay_peer(Tele(), "group: nobody here"))
    assert "no group with that name" in rd.relay_reason(LookupError("x"))
