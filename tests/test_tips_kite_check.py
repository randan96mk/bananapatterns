"""⚡ Tips → Kite orders PR C: the order check (code first, the AI read second), the 3:35 PM reconciliation, the Home
"Needs a look" items, the health row, the job's timing. The check never places, modifies or cancels anything."""

import json

from backend.database.models import get_db
from backend.tips import book as bk
from backend.tips import kite_check as kc
from backend.tips import kite_orders as ko
from backend.tips import track as tr
import pytest

from backend.tips import reader as rd
from tests.test_tips_kite_auto import CALL1, CALL2, S82200, S82300, post, sworld, t19, tick
from tests.tips_world import cfg


@pytest.fixture
def pushed(monkeypatch):
    out = []
    monkeypatch.setattr(bk, "push", lambda config, key, html: out.append(html) or True)
    monkeypatch.setattr(rd, "PUSH_MAX_AGE_S", 10 ** 9)
    return out


def dry_setup():
    c = cfg()
    c.tips.kite_mode = "dry"
    w = sworld(**{S82300: 309.0, S82200: 560.0})
    return c, w, ko.sim()


def items(h):
    return [x["text"] for x in (h["kite"]["check"] or {}).get("items") or []]


def test_a_clean_flow_passes_and_nothing_is_touched_by_the_check(pushed):
    c, w, s = dry_setup()
    hid = post(c, w, w, 1, "13:52", CALL2)["hint_id"]
    tr.poll(c, w, t19("13:52", 30))
    ko.place_all(c, s, hid, 1, now=t19("13:53"))
    tick(c, w, {S82300: 372.0}, "13:58")
    h = bk.load(hid)
    before = [dict(r) for r in ko.rows_for(hid)]
    res = kc.check(c, h, now=t19("14:00"))
    assert res["ok"] and res["red"] == 0
    assert [dict(r) for r in ko.rows_for(hid)] == before                    # the check never places / modifies / cancels
    assert not any(p.startswith("⚠️ Kite check") for p in pushed)


def test_wrong_qty_strike_and_missing_stop_are_red_with_the_hand_action(pushed, monkeypatch):
    c, w, s = dry_setup()
    hid = post(c, w, w, 1, "13:52", CALL2)["hint_id"]
    tr.poll(c, w, t19("13:52", 30))
    ko.place_entry(c, s, hid, 1, now=t19("13:53"))                            # the entry only: no stop yet
    tick(c, w, {S82300: 372.0}, "13:58")
    with get_db() as conn:                                                    # a deliberately wrong quantity on the row
        conn.execute("UPDATE tips_orders SET qty = 60 WHERE hint_id = ? AND leg = 'entry'", (hid,))
    h = bk.load(hid)
    res = kc.after_event(c, h, t19("14:00"))
    texts = [x["text"] for x in res["items"]]
    assert "entry qty 60 ≠ 1 lot × 20" in texts and "no stop resting" in texts and not res["ok"]
    hand = {x["text"]: x["hand"] for x in res["items"]}
    assert hand["no stop resting"] == "place the SL on Kite now"
    red = [p for p in pushed if p.startswith("⚠️ Kite check")]
    assert "⚠️ Kite check · #1 · no stop resting · place the SL on Kite now · your call" in red
    kc.after_event(c, bk.load(hid), t19("14:01"))                             # a red item is pushed once
    assert len([p for p in pushed if p.startswith("⚠️ Kite check")]) == len(red)
    # a wrong strike: the contract the hint resolved to is not the post's
    h = bk.load(hid)
    h["raw"] = h["raw"].replace("82300", "82500")
    res = kc.check(c, h, now=t19("14:02"))
    assert "the strike 82300 is not in the post" in [x["text"] for x in res["items"]]
    att = kc.attention_items(t19("14:03"))
    assert ("warn", "⚡ Kite #1 · no stop resting → place the SL on Kite now") in att
    from backend.engine import attention
    monkeypatch.setattr(kc, "_now", lambda: t19("14:03"))                  # Home reads "today": the test's day, not the wall clock
    sig = attention.signals(c, True)
    assert any("no stop resting" in x["text"] for x in sig["tips"]["items"])


def test_a_stop_is_given_60_seconds_after_the_fill(pushed):
    c, w, s = dry_setup()
    hid = post(c, w, w, 1, "13:52", CALL2)["hint_id"]
    tr.poll(c, w, t19("13:52", 30))
    ko.place_entry(c, s, hid, 1, now=t19("13:53"))
    tick(c, w, {S82300: 372.0}, "13:58")
    assert "no stop resting" not in items(bk.load(hid))                       # within 60 s: not red yet
    assert "no stop resting" in [x["text"] for x in kc.check(c, bk.load(hid), now=t19("13:59", 30))["items"]]


def test_ai_read_with_a_fake_model_drops_lines_with_figures_not_in_the_input(pushed):
    c, w, s = dry_setup()
    hid = post(c, w, w, 1, "13:52", CALL2)["hint_id"]
    tr.poll(c, w, t19("13:52", 30))
    ko.place_all(c, s, hid, 1, now=t19("13:53"))
    tick(c, w, {S82300: 372.0}, "13:58")
    seen = {}

    def fake(system, user):
        seen["system"], seen["user"] = system, user
        return ("1. The app rested a BUY stop at 370 and it filled at 370 with SL 200 and T1 500.\n"
                "2) It matches the channel: the level 370 decided, the activation words did not gate it.\n"
                "- Watch the stop at 999 and the 12345 lots.\n"
                "3. Look at the time stop at 3:20 PM.")
    out = kc.ai_read(c, hid, generate=fake, now=t19("14:00"))
    assert "SL 200 · limit 197" in seen["user"] and "WAIT FOR ACTIVATION" in seen["user"] and "Code check: all clear" in seen["user"]
    assert out["dropped"] == 1 and len(out["lines"]) == 3
    assert out["lines"][0].startswith("The app rested a BUY stop at 370") and not any("999" in x for x in out["lines"])
    k = bk.load(hid)["kite"]
    assert k["ai"]["state"] == "done" and k["ai"]["lines"] == out["lines"]
    v = tr.hint_view(bk.load(hid), config=c)["kite"]
    assert v["ai"]["lines"] == out["lines"] and v["check"]["ok"]
    with get_db() as conn:
        n = conn.execute("SELECT kind, prompt_version, text FROM ai_notes WHERE kind = 'tips_kite'").fetchall()
    assert len(n) == 1 and n[0][1] == "kite-check-v1"
    # the queue: AI on -> pending until the model writes (the service is unreachable in tests)
    c.ai.enabled = True
    kc.enqueue_ai(c, bk.load(hid))
    assert bk.load(hid)["kite"]["ai"]["state"] == "pending" and bk.load(hid)["kite"]["ai"]["lines"] == out["lines"]


def test_day_check_summary_and_panel(pushed):
    c, w, s = dry_setup()
    h1 = post(c, w, w, 1, "12:50", CALL1)["hint_id"]
    h2 = post(c, w, w, 2, "13:52", CALL2)["hint_id"]
    tr.poll(c, w, t19("13:52", 30))
    ko.place_all(c, s, h1, 1, now=t19("13:53"))
    ko.place_all(c, s, h2, 1, now=t19("13:53"))
    tick(c, w, {S82200: 538.0}, "13:55")                                       # call 1 fills; call 2 still resting
    res = kc.day_check(c, t19("15:35"))
    assert res["orders"] == 3 and res["positions"] == 1 and res["open"] == 1 and res["red"] >= 2
    assert res["targets_watched"] == 1 and res["targets_hit"] == 0                # the day check counts watched targets, not orders
    texts = [x["text"] for x in res["items"]]
    assert any(t.startswith("#2 DRY entry still open on Kite") for t in texts)
    assert any("open after the time stop" in t for t in texts)
    assert res["summary"] == f"🔎 Kite day · 3 orders · 1 position · 1 open · targets 1 watched · 0 hit · checks: {res['red']} red"
    assert any(p.startswith("🔎 Kite day · 3 orders") for p in pushed)
    assert kc.last_day()["summary"] == res["summary"] and ko.today(c, w, t19("15:36"))["day_check"]["summary"] == res["summary"]
    row = ko.health_row(c)
    assert "last check 3:35 PM" in row["state"] and f"{res['red']} red" in row["state"]


def test_day_check_all_clear_and_a_fill_with_no_hint(pushed):
    c, w, s = dry_setup()
    res = kc.day_check(c, t19("15:35"))
    assert res["summary"] == "🔎 Kite day · 0 orders · 0 positions · 0 open · targets 0 watched · 0 hit · checks: all clear"
    with get_db() as conn:
        conn.execute("INSERT INTO tips_orders (hint_id, leg, side, qty, order_type, status, order_id, filled_px, filled_qty, day, \"by\") "
                     "VALUES (77, 'entry', 'BUY', 20, 'MARKET', 'complete', 'X1', 300, 20, ?, 'click')", (t19("10:00").date().isoformat(),))
    res = kc.day_check(c, t19("15:40"), notify=False)
    assert "a fill with no hint (order X1)" in [x["text"] for x in res["items"]]


def test_the_job_runs_at_3_35_pm_on_trading_days():
    c = cfg()
    c.tips.kite_mode = "click"
    assert kc.due(t19("15:34"), c, set(), True) == []
    assert kc.due(t19("15:35"), c, set(), True) == ["tips_kite_day"]
    assert kc.due(t19("15:35"), c, {"tips_kite_day"}, True) == []
    assert kc.due(t19("15:35"), c, set(), False) == []
    c.tips.kite_mode = "off"
    assert kc.due(t19("15:35"), c, set(), True) == []                         # off and no order today: nothing to check


def test_strings_are_12_hour_and_json_safe(pushed):
    c, w, s = dry_setup()
    hid = post(c, w, w, 1, "13:52", CALL2)["hint_id"]
    tr.poll(c, w, t19("13:52", 30))
    ko.place_all(c, s, hid, 1, now=t19("13:53"))
    v = tr.hint_view(bk.load(hid), config=c)
    blob = json.dumps(v["kite"]["legs"]) + json.dumps(kc.day_check(c, t19("15:35"), notify=False))
    import re
    assert not re.search(r"\b(1[3-9]|2[0-3]):\d\d\b", blob.replace("T13:", "T").replace("T15:", "T"))


def test_the_ai_read_drops_empty_json_and_marks_the_state():
    from backend.tips import kite_check as kc
    assert kc.parse_lines("{}", "post 460") == ([], 0)
    assert kc.parse_lines("[]\n1. \n- 460", "post 460") == ([], 0)
    lines, dropped = kc.parse_lines("The app rested a BUY at 460.\n{}\nIt matches the post.", "post 460")
    assert lines == ["The app rested a BUY at 460.", "It matches the post."] and dropped == 0


def test_the_ai_read_asks_for_plain_text_not_json(monkeypatch):
    from backend.ai import client
    seen = {}

    def fake_generate(model, prompt, **kw):
        seen.update(kw)
        seen["prompt"] = prompt
        return {"text": "{}", "tokens": 1, "model": model}
    monkeypatch.setattr(client, "generate", fake_generate)
    import inspect
    from backend.tips import kite_check as kc
    src = inspect.getsource(kc.ai_read)
    assert "json_mode=False" in src and "plain text" in src
    assert "no JSON" in kc.SYSTEM
