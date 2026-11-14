"""🧩 Step 3 of the options module: the Telegram texts (the four shapes of the approved mock and the one-liners), the
scheduler sending each recorded alert once, the `opt` / `opt close` / `opt hold` commands, the attention items."""

from dataclasses import replace

from backend.alerts import jobs
from backend.alerts import messages as msg
from backend.alerts.commands import BotCommands
from backend.alerts.dispatch import Notifier
from backend.config import AlertsConfig, AppConfig
from backend.engine import attention
from backend.options import book
from tests.test_alerts import FakeTG
from tests.test_login import BotTG, update
from tests.test_options_book import DAY, World, at, minutes, path, run_day, seed_prev_day

BULL = {"09:30": 25050, "09:45": 25150, "09:55": 25250, "10:05": 25400, "10:30": 25450}
BROKERS = ("Zerodha", "Tradetron")

MOCK_OPEN = {"bias": "BULLISH", "sold": {"strike": 25900.0, "type": "PE", "entry": 48.5}, "hedge": {"strike": 25650.0, "type": "PE", "entry": 12.0},
             "lots": 1, "credit_rs": 2737.5, "stop_premium": 54.75, "stop_pct": 50.0, "target_value": 14.6, "target_pct": 60.0, "worst_case": 13350.0,
             "margin_est": 52000.0, "sessions": 4, "vix": 13.4, "range": {"low": 25980.0, "high": 26118.0}, "prev": {"high": 26100.0, "low": 25800.0},
             "vwap_at_entry": 26094.0, "close": 26131.0, "opened_t": "9:52 AM", "n": 1,
             "chain_line": "support 25,800 · resistance 26,300 · put OI building · PCR 1.24 → 3/3"}
MOCK_TRADE = {"sold": {"strike": 25900.0, "type": "PE", "entry": 48.5}, "hedge": {"strike": 25650.0, "type": "PE", "entry": 12.0}, "cost": 14.4,
              "net": 2558.0, "lots": 1, "opened_at": "2026-11-14T09:52:00+05:30", "closed_at": "2026-11-14T12:43:00+05:30", "be_active": True,
              "be_t": "10:41 AM", "why": "target", "est": False}


def no_brokers(t: str) -> bool:
    return not any(b in t for b in BROKERS)


# --- the texts ---------------------------------------------------------------------------------------------------------

def test_inr_indian_grouping():
    assert msg.inr(306800) == "₹3,06,800" and msg.inr(52000) == "₹52,000" and msg.inr(2738) == "₹2,738" and msg.inr(999) == "₹999"
    assert msg.inr(-13350, sign=True) == "−₹13,350" and msg.inr(2558, sign=True) == "+₹2,558" and msg.inr(12345678.5, 2) == "₹1,23,45,678.50"
    assert msg.inr(None) == "—" and msg.inr(0, sign=True) == "+₹0"


def test_bias_message_is_the_mock():
    t = msg.options_bias(MOCK_OPEN)
    lines = t.split("\n")
    assert lines[0] == "🧭 <b>Options · paper · bias BULLISH · 9:52 AM</b>"
    assert lines[1] == "NIFTY 26,131 above the 9:15–9:30 range (25,980–26,118) and VWAP 26,094 · prev high taken"
    assert lines[2] == "SELL <b>25,900 PE</b> @ 48.50 · BUY 25,650 PE @ 12.00 · 1 lot · credit ₹2,738"
    assert lines[3] == "stop 54.75 (+50%) or a 5-min close under VWAP · target 14.60 (60%) · worst case −₹13,350"
    assert lines[4] == "<i>margin ~₹52,000 · 4 sessions to expiry · VIX 13.4</i>" and lines[5].startswith("<i>chain: support 25,800")
    assert no_brokers(t)
    bear = msg.options_bias({**MOCK_OPEN, "bias": "BEARISH", "sold": {"strike": 26300.0, "type": "CE", "entry": 40.0}, "lots": 2, "sessions": 0, "n": 2})
    assert "below the 9:15–9:30 range" in bear and "prev low taken" in bear and "2 lots" in bear and "expiry day" in bear and "re-entry" in bear
    assert "close over VWAP" in bear and "26,300 CE" in bear


def test_exit_messages():
    t = msg.options_exit("target", MOCK_TRADE)
    assert t == "🎯 <b>Options · paper · target</b> · 25,900 / 25,650 PE closed at 14.40 · <b>+₹2,558</b> · held 2h 51m · breakeven stop was armed at 10:41 AM"
    stop = msg.options_exit("premium stop", {**MOCK_TRADE, "net": -4120.0, "cost": 51.2, "be_active": False, "closed_at": "2026-11-14T10:17:00+05:30"})
    assert stop.startswith("🛑 <b>Options · paper · premium stop</b>") and "−₹4,120" in stop and "held 25m" in stop and "breakeven" not in stop
    assert "VWAP stop" in msg.options_exit("vwap stop", MOCK_TRADE)
    be = msg.options_exit("breakeven stop", MOCK_TRADE)
    assert "breakeven stop</b>" in be and "armed at 10:41 AM" in be and "was armed" not in be
    te = msg.options_exit("time exit", {**MOCK_TRADE, "lots": 2, "est": True})
    assert te.startswith("⏱ <b>Options · paper · time exit</b>") and "2 lots" in te and "last known mid" in te
    assert msg.options_exit("kill", MOCK_TRADE).startswith("🔴") and "kill switch" in msg.options_exit("kill", MOCK_TRADE)
    assert msg.options_exit("closed by you", MOCK_TRADE).startswith("✋") and "closed by you" in msg.options_exit("closed by you", MOCK_TRADE)
    assert "AM" in t and "10:41" in t and ":41 " in t and no_brokers(t)


def test_no_trade_and_summary_messages():
    t = msg.options_no_trade({"range": {"low": 25990.0, "high": 26060.0}, "vwap": 26020.0, "vwap_where": "in the middle", "window_end": "11:00",
                              "window_start": "09:45", "next": "2026-11-16"})
    assert t == "🧭 <b>Options · no trade today</b> · no break of the 9:15–9:30 range by 11:00 AM (25,990–26,060 · VWAP in the middle) · next look Mon 9:45 AM"
    s = msg.options_summary({"day": "2026-11-13", "trades": 1, "pnl": 2558.0, "result": "target"},
                            {"equity": 306800.0, "return": 2.3, "started": "2026-12-01", "days": 11, "traded": 8, "won": 5})
    assert s == "🌙 <b>Options · paper · Fri 13 Nov</b> · 1 trade · <b>+₹2,558</b> · target · book ₹3,06,800 (+2.3% since 1 Dec) · 8 traded of 11 days · won 63%"
    s0 = msg.options_summary({"day": "2026-11-16", "trades": 0, "pnl": 0.0, "result": "no break by 11:00 AM", "why": "no break by 11:00 AM"},
                             {"equity": 300000.0, "return": 0.0, "started": "2026-11-16", "days": 1, "traded": 0, "won": 0})
    assert "no trade (no break by 11:00 AM)" in s0 and "book ₹3,00,000 (+0.0% since 16 Nov)" in s0 and "0 traded of 1 day" in s0 and "won" not in s0


def test_one_liners():
    assert "chain disagrees" in msg.options_chain_disagrees({"bias": "BEARISH", "line": "PCR 1.3 → 0/3"}) and "0/3" in msg.options_chain_disagrees({"bias": "BEARISH", "line": "PCR 1.3 → 0/3"})
    dl = msg.options_daily_loss({"used": 4800.0, "limit": 4500.0, "hit": True})
    assert "daily loss limit reached" in dl and "−₹4,800" in dl and "−₹4,500" in dl and "keeps its stops" in dl
    ev = msg.options_skipped("event_day", {"bias": "BULLISH", "reasons": ["event day"]})
    assert "event day · no entry" in ev and "BULLISH found" in ev
    assert "VIX 21.3 over 20" in msg.options_vix_half({"vix": 21.3, "half_at": 20.0, "lots": 1}) and "1 lot today" in msg.options_vix_half({"vix": 21.3, "half_at": 20.0, "lots": 1})
    assert "chain score 1 · half size" in msg.options_no_size("no_trade_size", {"bias": "BULLISH", "notes": ["chain score 1 · half size"]})
    assert "premium under 20" in msg.options_no_size("no_trade_strikes", {"bias": "BEARISH", "why": "premium under 20"})
    nd = msg.options_no_data({"why": "Kite login missing"})
    assert "no data" in nd and "Kite login missing" in nd and "log in from the dashboard" in nd and no_brokers(nd)
    plain = msg.options_alert({"kind": "something_new", "t": "9:31 AM", "text": "a text", "tag": "paper"})
    assert plain == "🧩 <b>Options · paper · 9:31 AM</b> · a text"
    assert msg.options_alert({"kind": "entry", "t": "9:52 AM", "text": "x", "tag": "paper", "data": MOCK_OPEN}).startswith("🧭 <b>Options · paper · bias BULLISH · 9:52 AM")
    assert msg.options_alert({"kind": "entry", "t": "9:52 AM", "text": "fallback", "tag": "paper", "data": None}).endswith("· fallback")


# --- the scheduler sends each alert once ------------------------------------------------------------------------------

class St:
    def __init__(self, w, cfg=None):
        self.config = cfg or AppConfig()
        self.config.alerts = AlertsConfig(chat_id="42", intraday=False, filings=False)
        self.client = w
        self.token_source = "db"

    def token_valid(self):
        return self.client.has_token


def scheduler(st, fake):
    return jobs.Scheduler(st, notifier_factory=lambda cfg: Notifier(cfg, fake))


def run_sched(sch, w, start="09:15", end="15:40", until=None):
    for t in minutes(start, end):
        w.now = t
        sch.run_options_tick(at(t))
        if until and until(book._state()):
            break


def test_tick_sends_every_alert_once():
    seed_prev_day()
    w = World(path(BULL))
    st, fake = St(w), FakeTG()
    sch = scheduler(st, fake)
    book.start(st.config, at("09:15"))
    run_sched(sch, w)
    state = book._state()
    kinds = [a["kind"] for a in state["alerts"]]
    assert kinds == ["entry", "exit", "summary"] and len(fake.sent) == 3
    bias, exit_, summ = (h for _, h in fake.sent)
    assert bias.startswith("🧭 <b>Options · paper · bias BULLISH · 9:50 AM</b>") and "SELL <b>24,800 PE</b>" in bias and "BUY 24,550 PE" in bias
    assert "2 lots" in bias and "above the 9:15–9:30 range (25,000–25,100)" in bias and "prev high taken" in bias and "5 sessions to expiry" in bias
    assert "VIX 14.0" in bias and "chain:" in bias and "→ 3/3" in bias and "(+50%)" in bias and "(60%)" in bias
    t = book.trades()[0]
    assert exit_.startswith("🎯 <b>Options · paper · target</b> · 24,800 / 24,550 PE closed at") and msg.inr(t["net"], sign=True) in exit_
    assert "held" in exit_ and "breakeven stop was armed at" in exit_ and t["be_t"] in exit_
    assert summ.startswith("🌙 <b>Options · paper · Tue 17 Nov</b> · 1 trade") and "1 traded of 1 day" in summ and "won 100%" in summ and "since 17 Nov" in summ
    assert all(no_brokers(h) and "paper" in h for _, h in fake.sent) and all(" AM" in h or " PM" in h for _, h in fake.sent[:1])
    # another minute, a new scheduler on the same DB (a restart), the whole day again: nothing goes twice
    sch.run_options_tick(at("15:41"))
    scheduler(st, fake).run_options_tick(at("15:42"))
    assert len(fake.sent) == 3
    assert len(sch.options_sent) == 3 and all(k.startswith(f"{DAY.isoformat()}|opt-") for k in sch.options_sent)


def test_no_trade_day_sends_the_no_trade_line_and_the_summary():
    seed_prev_day()
    w = World(path({"09:30": 25050}))
    st, fake = St(w), FakeTG()
    sch = scheduler(st, fake)
    book.start(st.config)
    run_sched(sch, w)
    assert [a["kind"] for a in book._state()["alerts"]] == ["no_trade", "summary"] and len(fake.sent) == 2
    nt = fake.sent[0][1]
    assert nt.startswith("🧭 <b>Options · no trade today</b> · no break of the 9:15–9:30 range by 11:00 AM (25,000–25,100") and "next look Wed 9:45 AM" in nt
    assert "VWAP" in nt and "no trade (no break by 11:00 AM)" in fake.sent[1][1] and "0 traded of 1 day" in fake.sent[1][1]


def test_no_login_alerts_once_and_a_quiet_notifier_sends_nothing():
    seed_prev_day()
    w = World(path({}), has_token=False)
    st, fake = St(w), FakeTG()
    sch = scheduler(st, fake)
    book.start(st.config)
    run_sched(sch, w, end="09:40")
    assert len(fake.sent) == 1 and fake.sent[0][1].startswith("⚠️ <b>Options · paper · no data</b> · Kite login missing")
    run_sched(sch, w, start="09:41", end="10:00")
    assert len(fake.sent) == 1
    # Telegram off: the alert is recorded for the page, nothing is sent, and the minute stays cheap
    book.reset()
    quiet = FakeTG()
    sch2 = jobs.Scheduler(St(w, AppConfig()), notifier_factory=lambda cfg: Notifier(cfg, quiet))
    book.start(sch2.state.config)
    run_sched(sch2, w, end="09:35")
    assert quiet.sent == [] and [a["kind"] for a in book._state()["alerts"]] == ["no_data"] and len(sch2.options_sent) == 1


# --- the commands --------------------------------------------------------------------------------------------------------

def bot(w, updates, when):
    st, fake = St(w), BotTG(updates)
    return BotCommands(st, clock=lambda: when), Notifier(st.config.alerts, fake), fake, st


def test_opt_status_close_and_hold_commands():
    seed_prev_day()
    cfg = AppConfig()
    cfg.options = replace(cfg.options, capital=300000.0)
    w = World(path({"09:30": 25050, "09:45": 25150}))
    run_day(cfg, w, until=lambda s: s["open"])
    when = at("10:12")
    w.now = "10:12"
    b, n, fake, st = bot(w, [update(1, "opt", at=when), update(2, "opt close", at=when), update(3, "opt close", at=when),
                             update(4, "OPT HOLD", at=when), update(5, "/opt", at=when), update(6, "opt", chat=999, at=when)], when)
    st.config.options = cfg.options
    assert b.poll_once(n) == 5 and len(fake.sent) == 5
    status, closed, again, hold, status2 = (h for _, h in fake.sent)
    assert status.startswith("🧩 <b>Options · paper · Tue 17 Nov</b> · open") and "open: BULLISH · SELL 24,800 PE @" in status and "P&L" in status
    assert "opt close" in status and "opt hold" in status and "2 lots" in status
    assert closed.startswith("✋ <b>Options · paper · closed by you</b> · 24,800 / 24,550 PE closed at") and book._state()["open"] is None
    assert book.trades()[0]["why"] == "closed by you" and book.trades()[0]["out"] == "10:12 AM"
    assert again == "Not changed: no spread is open."
    assert hold.startswith("⏸ No new options entries today") and book._state()["day"]["hold"] is True and "hold today" in hold
    assert "no spread open" in status2 and "1 trade" in status2 and no_brokers(status) and no_brokers(status2)
    # the hold holds: the bias flips later, the book skips the entry
    w.now = "10:35"
    book.tick(cfg, w, at("10:35"))
    assert book._state()["open"] is None and book._state()["day"]["trades"] == 1


def test_opt_hold_before_the_book_started_and_help_lists_it():
    when = at("09:00")
    w = World(path({}))
    b, n, fake, st = bot(w, [update(1, "opt hold", at=when), update(2, "/help", at=when)], when)
    assert b.poll_once(n) == 2
    assert fake.sent[0][1].startswith("⏸ No new options entries today") and book._state()["day"]["hold"] is True and "no spread open" in fake.sent[0][1]
    assert "opt / opt close / opt hold" in fake.sent[1][1]


# --- the attention items -------------------------------------------------------------------------------------------------

def test_attention_items():
    seed_prev_day()
    cfg = AppConfig()
    assert attention.signals(cfg, True)["options"] == {"level": None, "items": [], "sig": ""}          # no book yet
    w = World(path({"09:30": 25050, "09:45": 25150}))
    run_day(cfg, w, until=lambda s: s["open"])
    now = at("10:00")
    assert attention._options(cfg, True, now) == []                                                    # an open spread far from its stop: quiet
    st = book._state()
    o = st["open"]
    s = o["sold"]
    s["last"] = round(s["entry"] + 0.85 * (o["stop_premium"] - s["entry"]), 2)
    book._save(st)
    items = attention._options(cfg, True, now)
    assert [lvl for lvl, _ in items] == ["sell"] and "near its stop" in items[0][1] and "24,800 PE" in items[0][1] and f"stop {o['stop_premium']:.2f}" in items[0][1]
    s["last"] = round(s["entry"] + 0.7 * (o["stop_premium"] - s["entry"]), 2)
    book._save(st)
    assert attention._options(cfg, True, now) == []
    assert [lvl for lvl, _ in attention._options(cfg, False, now)] == ["warn"]                        # no login with a spread open
    st["day"]["realised"] = -5000.0                                                                   # the daily loss limit (1.5% of 3,00,000 = 4,500)
    book._save(st)
    texts = [t for _, t in attention._options(cfg, True, now)]
    assert any("daily loss limit" in t for t in texts)
    book.set_killed(True, cfg, w, now)
    items = attention._options(cfg, True, now)
    assert items[0][0] == "warn" and "kill switch ON" in items[0][1] and book._state()["open"] is None
    book.set_killed(False, cfg, w, now)
    # no data on a trading day
    book.reset()
    w2 = World(path({}), has_token=False)
    run_day(cfg, w2, end="09:35")
    items = attention._options(cfg, False, now)
    assert items == [("warn", "Options: no data · Kite login missing")]
    assert attention._options(cfg, False, at("10:00", DAY.replace(day=22))) == []                      # a Sunday / another day: today's items only
    out = attention.signals(cfg, False)
    assert "options" in out and set(out["options"]) == {"level", "items", "sig"}
